#!/usr/bin/env python3
"""Submit the pinned FP16 graph with changing input via the native accel API.

The kernel accepts generic prepared images; this CLI qualifies the one existing
compiler artifact. No Windows runtime, CPU fallback or automatic retry.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from n1x_dla import Device

TEMPLATE_SHA256 = 'b5792ecfbdc5d190fec9cdf7ecc17c7ba6919a42d5c1528e2acbcc2fc47f997c'
# Firmware task-local order; logical lengths from the checked 0.9.0 loadable.
RANGES = [(16384, 256), (8192, 3136), (12288, 3136), (20480, 30464),
          (53248, 320), (57344, 2738944), (2797568, 28736), (2830336, 4096)]


def prepare(template, input_bytes):
    if len(template) != 2834432 or hashlib.sha256(template).hexdigest() != TEMPLATE_SHA256:
        raise ValueError('template differs from the hardware-verified artifact')
    if len(input_bytes) != 3136:
        raise ValueError('input must be 1568 FP16 values in CHW16 layout')
    # Restrict this numerical acceptance check to finite, nonzero inputs;
    # signed-zero, NaN and denormal behavior needs separate qualification.
    words = struct.unpack('<1568H', input_bytes)
    if any((word & 0x7c00) in (0, 0x7c00) for word in words):
        raise ValueError('acceptance inputs must be normal finite FP16 numbers')
    expected = struct.pack('<1568H', *(0 if word & 0x8000 else word for word in words))
    # The kernel supplies the first 8192 bytes: descriptor, fence, address list.
    image = bytearray(template[8192:])
    image[:3136] = input_bytes
    image[4096:4096+3136] = b'\xa5' * 3136
    return image, [(offset-8192, size) for offset, size in RANGES], expected


def save_record(path, record):
    with path.open('x') as output:
        output.write(json.dumps(record, indent=2)+'\n')
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--input', type=Path, nargs='+', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    template = args.template.read_bytes()
    # Validate all inputs before opening the device. A fresh output directory
    # prevents accidental re-use/overwrite of earlier experimental evidence.
    prepared = [prepare(template, path.read_bytes()) for path in args.input]
    args.output_dir.mkdir(exist_ok=False)
    with Device(args.device) as device:
        info = device.info()
        required = sum((len(image)+8192+4095) & ~4095 for image, _, _ in prepared)
        if required > info['retain_limit'] - info['retained_bytes'] or len(prepared) > info['max_jobs'] - info['retained_jobs']:
            raise RuntimeError('insufficient retained-job budget; no jobs submitted')
        for index, (image, ranges, expected) in enumerate(prepared):
            started = time.monotonic_ns()
            input_hash = hashlib.sha256(image[:3136]).hexdigest()
            save_record(args.output_dir / f'{index:03d}-attempt.json',
                        dict(input_sha256=input_hash, started_ns=started, submitted=False))
            job_id = 0
            try:
                job_id = device.submit(image, ranges)
                save_record(args.output_dir / f'{index:03d}-submitted.json', dict(job_id=job_id))
                output = device.read(job_id, 4096, len(expected))
                unchanged = device.read(job_id, 0, 3136) == image[:3136]
            except OSError as exc:
                save_record(args.output_dir / f'{index:03d}-error.json',
                            dict(job_id=getattr(exc, 'job_id', job_id), errno=exc.errno,
                                 error=str(exc), automatic_retry=False,
                                 submission_state_uncertain=True))
                raise
            (args.output_dir / f'{index:03d}.fp16').write_bytes(output)
            record = dict(job_id=job_id, elapsed_ns=time.monotonic_ns()-started,
                          input_sha256=hashlib.sha256(image[:3136]).hexdigest(),
                          output_sha256=hashlib.sha256(output).hexdigest(),
                          numerical_match=output == expected, input_unchanged=unchanged)
            save_record(args.output_dir / f'{index:03d}.json', record)
            print(json.dumps(record), flush=True)
            if not record['numerical_match'] or not unchanged:
                raise RuntimeError('numerical/input check failed; stopped without retry')


if __name__ == '__main__':
    main()
