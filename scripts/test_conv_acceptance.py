#!/usr/bin/env python3
"""Exercise convolution acceptance, durable failures and no-retry behavior.

Device.read returns explicit test fixtures, never a physical inference result.
Independent ONNX reference files must have been generated first.
"""
from contextlib import redirect_stdout
import errno
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import argparse
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
import run_conv
from loadable import LoadableError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relu',action='store_true')
    args=parser.parse_args()
    name='conv1x1_relu' if args.relu else 'conv1x1'
    fixture_dir=ROOT/('results/conv-relu-probe' if args.relu else 'results/conv-probe')
    model_path = ROOT/f'results/native-compile-t254-{name}/{name}.nvda'
    model = run_conv.load_model(model_path)
    source, target = model.inputs[0], model.outputs[0]
    constants, constant_size = model.ranges[2]
    fixtures = run_conv.inputs()
    independent = [(fixture_dir/f'{name}-expected.fp16').read_bytes() for name in fixtures]
    for data, expected in zip(fixtures.values(), independent):
        image, ranges, reference = run_conv.prepare(model, data)
        assert reference == expected and ranges == model.ranges
        assert image[target.offset:target.offset+3136] == struct.pack('<H', 0x7e55)*1568
        assert image[constants:constants+constant_size] == model.image[constants:constants+constant_size]
    for bad in (b'', bytes(3136), b'\0'*3137, struct.pack('<H', 0x7c00)*1568,
                struct.pack('<H', 0x7e00)*1568, struct.pack('<H', 1)*1568,
                struct.pack('<1568e', *([1/32]*1568))):
        try:
            run_conv.prepare(model, bad)
        except LoadableError:
            pass
        else:
            raise AssertionError('invalid numerical input accepted')
    results = []
    with tempfile.TemporaryDirectory(prefix='n1x-conv-acceptance-') as directory:
        base = Path(directory)
        paths = []
        for name, data in fixtures.items():
            path = base/(name+'.fp16')
            path.write_bytes(data)
            paths.append(path)
        for mode in ('success', 'poison', 'wrong_value', 'stale_second', 'input_changed',
                     'constants_changed', 'submit_timeout', 'read_failure', 'budget'):
            calls, images = [], []

            class FakeDevice:
                def __init__(self, path):
                    assert path == '/dev/modeled-only'
                    calls.append('open')
                def __enter__(self): return self
                def __exit__(self, *_): calls.append('close')
                def info(self):
                    return dict(retain_limit=1 if mode == 'budget' else 64 << 20,
                                retained_bytes=0, max_jobs=128, retained_jobs=0)
                def submit(self, image, ranges, writable):
                    calls.append('submit')
                    assert ranges == model.ranges
                    assert writable == [(tensor.offset, tensor.bytes)
                                        for tensor in [*model.inputs, *model.outputs]]
                    images.append(bytes(image))
                    if mode == 'submit_timeout':
                        error = OSError(errno.ETIMEDOUT, 'modeled submission failure')
                        error.job_id = 19
                        raise error
                    return 19+len(images)-1
                def read(self, job, offset, size):
                    calls.append('read')
                    at = job-19
                    assert 0 <= at < len(images)
                    data = images[at][offset:offset+size]
                    if mode == 'read_failure':
                        raise OSError(errno.EIO, 'modeled read failure')
                    if offset == target.offset:
                        assert size == 3136
                        if mode == 'poison': return data
                        if mode == 'wrong_value': return bytes(size)
                        if mode == 'stale_second' and at == 1: return independent[0]
                        return independent[at]
                    assert (offset, size) in ((source.offset, 3136), (constants, constant_size))
                    if (mode == 'input_changed' and offset == source.offset or
                            mode == 'constants_changed' and offset == constants):
                        return bytes([data[0]^1])+data[1:]
                    return data

            output = base/mode
            argv = ['run_conv', '--model', str(model_path), '--device', '/dev/modeled-only',
                    '--input', *map(str, paths), '--output-dir', str(output)]
            failure = None
            with patch.object(run_conv, 'Device', FakeDevice), patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                try:
                    run_conv.main()
                except (LoadableError, OSError) as exc:
                    failure = exc
            assert calls[-1] == 'close'
            if mode == 'success':
                assert failure is None and calls.count('submit') == 3
                for i, expected in enumerate(independent):
                    record = json.loads((output/f'{i:03d}.json').read_text())
                    assert record['numerical_match'] and record['input_unchanged'] and record['constants_unchanged']
                    assert (output/f'{i:03d}.fp16').read_bytes() == expected
            elif mode == 'budget':
                assert isinstance(failure, LoadableError) and calls == ['open', 'close']
                assert not list(output.glob('*.attempt.json'))
            else:
                count = 2 if mode == 'stale_second' else 1
                assert failure is not None and calls.count('submit') == count
                assert len(list(output.glob('*.attempt.json'))) == count
                assert not (output/f'{count:03d}.attempt.json').exists()
                if mode in ('submit_timeout', 'read_failure'):
                    record = json.loads((output/'000.error.json').read_text())
                    assert record['job_id'] == 19 and not record['automatic_retry']
                    assert not (output/'000.json').exists()
                else:
                    record = json.loads((output/f'{count-1:03d}.json').read_text())
                    assert not all(record[key] for key in ('numerical_match', 'input_unchanged', 'constants_unchanged'))
            results.append(dict(case=mode, submits=calls.count('submit'),
                                rejected=failure is not None, device_modeled=True))
        # Validation of every input must finish before device opening/submission.
        invalid = base/'invalid.fp16'
        invalid.write_bytes(bytes(3136))
        with patch.object(run_conv, 'Device') as device, patch.object(sys, 'argv',
                ['run_conv', '--model', str(model_path), '--device', '/dev/modeled-only',
                 '--input', str(paths[0]), str(invalid), '--output-dir', str(base/'invalid')]):
            try:
                run_conv.main()
            except LoadableError:
                pass
            else:
                raise AssertionError('invalid later input accepted')
            device.assert_not_called()
            assert not (base/'invalid').exists()
    print(json.dumps(dict(passed=True, hardware_access=False, cases=results,
                         invalid_input_cases=7, preflight_before_device_open=True,
                         independent_onnx_reference_match=True), indent=2))


if __name__ == '__main__':
    main()
