#!/usr/bin/env python3
"""Run a trusted, supported N1X NVDA loadable via the native accel interface.

Inputs and outputs are raw packed tensor bytes; names/shapes/strides can be
inspected with loadable.py. Completion is not a numerical accuracy claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from loadable import Model, require
from n1x_dla import Device


def record(path, data):
    with path.open('x') as out:
        out.write(json.dumps(data, indent=2)+'\n')
        out.flush()
        os.fsync(out.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--input', action='append', default=[], metavar='NAME=FILE')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    model = Model.from_file(args.model)
    paths = {}
    for item in args.input:
        name, separator, path = item.partition('=')
        require(separator and name and path and name not in paths, 'invalid or duplicate input argument')
        paths[name] = Path(path)
    require(set(paths) == {t.name for t in model.inputs}, 'input names do not match model')
    inputs = {}
    for tensor in model.inputs:
        with paths[tensor.name].open('rb') as source:
            inputs[tensor.name] = source.read(tensor.bytes+1)
    image, ranges = model.prepare(inputs)
    args.output_dir.mkdir(exist_ok=False)
    record(args.output_dir/'model.json', model.describe())
    job_id = 0
    with Device(args.device) as device:
        info = device.info()
        needed = (len(image)+8192+4095) & ~4095
        require(needed <= info['retain_limit']-info['retained_bytes'] and
                info['retained_jobs'] < info['max_jobs'], 'retained job budget exhausted')
        started = time.monotonic_ns()
        record(args.output_dir/'attempt.json', dict(started_ns=started, model_sha256=model.sha256,
               inputs={name: hashlib.sha256(value).hexdigest() for name, value in inputs.items()}))
        try:
            job_id = device.submit(image, ranges)
            record(args.output_dir/'submitted.json', dict(job_id=job_id))
            outputs = []
            for index, tensor in enumerate(model.outputs):
                data = device.read(job_id, tensor.offset, tensor.bytes)
                # Never use a model-controlled tensor name as a filesystem path.
                path = args.output_dir/f'{index:03d}.bin'
                with path.open('xb') as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                outputs.append(dict(name=tensor.name, file=path.name, bytes=len(data),
                                    sha256=hashlib.sha256(data).hexdigest()))
        except OSError as exc:
            record(args.output_dir/'error.json', dict(job_id=getattr(exc, 'job_id', job_id),
                   errno=exc.errno, error=str(exc), submission_state_uncertain=True, automatic_retry=False))
            raise
        result = dict(job_id=job_id, completed=True, numerical_accuracy_verified=False,
                      elapsed_ns=time.monotonic_ns()-started, outputs=outputs)
        record(args.output_dir/'result.json', result)
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
