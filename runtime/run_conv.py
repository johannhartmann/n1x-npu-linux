#!/usr/bin/env python3
"""Qualify pinned FP16 1x1 convolution or Conv->ReLU through native accel.

CPU calculations produce comparison data only; results come from Device.read.
Inputs are odd multiples of 1/16 in [-31/16, 31/16], giving exact, nonzero
normal convolution outputs before optional ReLU. General rounding is not tested.
"""
import argparse
import hashlib
import os
from pathlib import Path
import struct
import time

from loadable import Model, require
from n1x_dla import Device
from run_loadable import record

LOADABLE_HASH = '717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b'
CONV_RELU_HASH = 'fe57704f93edbafe9e374e14675cfc34cc5a44858497c6126dec56a60a837536'
SHAPE = (1, 32, 7, 7)
BYTES = 3136


def index(c, h, w):
    """FP16 element index in N,C/16,H,W,16 for the fixed batch-one tensor."""
    return ((c//16*7+h)*7+w)*16+c%16


def inputs():
    """Distinct channel/spatial patterns, all exactly representable in FP16."""
    cases = {}
    for name, coefficients in (('channels', (13, 7, 3)), ('spatial', (17, 5, 11))):
        values = [0]*1568
        a, b, d = coefficients
        for c in range(32):
            for h in range(7):
                for w in range(7):
                    values[index(c, h, w)] = (2*((a*c+b*h+d*w)%32)-31)/16
        cases[name] = struct.pack('<1568e', *values)
    cases['negative_channels'] = struct.pack('<1568e', *(
        -v for v in struct.unpack('<1568e', cases['channels'])))
    return cases


def expected_output(data, relu=False):
    require(len(data) == BYTES, 'input must contain 1568 packed FP16 values')
    values = struct.unpack('<1568e', data)
    # Set membership rejects NaN, infinity, zero, subnormals and arbitrary
    # rounded values before converting to integer numerators.
    allowed = {n/16 for n in range(-31, 32, 2)}
    require(all(v in allowed for v in values), 'input must use odd multiples of 1/16 in [-31/16,31/16]')
    numerators = [int(v*16) for v in values]
    result = [0]*1568
    for c in range(32):
        for h in range(7):
            for w in range(7):
                at = index(c, h, w)
                # Exact integer reference in units of 1/128, from the source
                # graph (diagonal + adjacent channel + bias), not backend code.
                n = 2*(c%3+1)*numerators[at]-numerators[index((c+1)%32, h, w)]+4*c-64
                assert n != 0
                result[at] = max(0,n)/128 if relu else n/128
    encoded = struct.pack('<1568e', *result)
    assert tuple(result) == struct.unpack('<1568e', encoded)
    return encoded


def load_model(path):
    model = Model.from_file(path)
    require(model.sha256 in (LOADABLE_HASH,CONV_RELU_HASH), 'model differs from checked convolution artifacts')
    require(len(model.inputs) == len(model.outputs) == 1, 'unexpected I/O count')
    for tensor in (*model.inputs, *model.outputs):
        require(tensor.shape == SHAPE and tensor.bytes == BYTES and
                tensor.format_codes == (3, 2, 2, 36, 0) and
                tensor.strides == (2, 224, 1568, 0, 3136, 0, 0, 0), 'unexpected tensor layout')
    start, size = model.ranges[2]
    require(size == 8192, 'unexpected constant allocation')
    kernel = struct.unpack('<1024e', model.image[start:start+2048])
    require(kernel == tuple((c%3+1)/4 if j == c else -0.125 if j == (c+1)%32 else 0
                            for c in range(32) for j in range(32)), 'kernel differs from graph')
    # Observed FP19 words: one sign bit, eight exponent bits, ten fraction
    # bits in the low 19 bits of each u32. All fixture biases are exact.
    bias_words = struct.unpack('<32I', model.image[start+4096:start+4224])
    expected_bias = tuple(struct.unpack('<I', struct.pack('<f', c/32-0.5))[0] >> 13 for c in range(32))
    require(bias_words == expected_bias, 'FP19 bias differs from graph')
    return model


def prepare(model, data):
    expected = expected_output(data, relu=model.sha256==CONV_RELU_HASH)
    image, ranges = model.prepare({model.inputs[0].name: data})
    tensor = model.outputs[0]
    # qNaN poison can never equal either fixture's finite reference.
    image[tensor.offset:tensor.offset+BYTES] = struct.pack('<H', 0x7e55)*1568
    return image, ranges, expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--input', type=Path, nargs='+', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    model = load_model(args.model)
    prepared = []
    for path in args.input:
        with path.open('rb') as source:
            prepared.append(prepare(model, source.read(BYTES+1)))
    args.output_dir.mkdir(exist_ok=False)
    record(args.output_dir/'model.json', model.describe())
    with Device(args.device) as device:
        info = device.info()
        required = sum((len(image)+8192+4095) & ~4095 for image, _, _ in prepared)
        require(required <= info['retain_limit']-info['retained_bytes'] and
                len(prepared) <= info['max_jobs']-info['retained_jobs'],
                'insufficient retained job budget; no jobs submitted')
        for number, (image, ranges, expected) in enumerate(prepared):
            prefix = args.output_dir/f'{number:03d}'
            started = time.monotonic_ns()
            source = model.inputs[0]
            source_bytes = bytes(image[source.offset:source.offset+BYTES])
            record(prefix.with_suffix('.attempt.json'), dict(started_ns=started,
                   input_sha256=hashlib.sha256(source_bytes).hexdigest(),
                   expected_sha256=hashlib.sha256(expected).hexdigest()))
            job_id = 0
            try:
                job_id = device.submit(image, ranges)
                record(prefix.with_suffix('.submitted.json'), dict(job_id=job_id))
                output = device.read(job_id, model.outputs[0].offset, BYTES)
                unchanged = device.read(job_id, source.offset, BYTES) == source_bytes
                constant_start, constant_size = ranges[2]
                constants_unchanged = device.read(job_id, constant_start, constant_size) == image[constant_start:constant_start+constant_size]
            except OSError as exc:
                record(prefix.with_suffix('.error.json'), dict(job_id=getattr(exc, 'job_id', job_id),
                       errno=exc.errno, error=str(exc), automatic_retry=False,
                       submission_state_uncertain=True))
                raise
            with prefix.with_suffix('.fp16').open('xb') as out:
                out.write(output)
                out.flush()
                os.fsync(out.fileno())
            result = dict(job_id=job_id, elapsed_ns=time.monotonic_ns()-started,
                          output_sha256=hashlib.sha256(output).hexdigest(),
                          numerical_match=output == expected, input_unchanged=unchanged,
                          constants_unchanged=constants_unchanged)
            record(prefix.with_suffix('.json'), result)
            require(result['numerical_match'] and unchanged and constants_unchanged,
                    'numerical/input/constants check failed; stopped without retry')
            print(f'job {job_id}: exact convolution output, input and constants verified', flush=True)


if __name__ == '__main__':
    main()
