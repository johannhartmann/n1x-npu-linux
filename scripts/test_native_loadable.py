#!/usr/bin/env python3
"""Cross-check the native loader against independent fixtures and C materializer."""
import ctypes
from contextlib import redirect_stdout
import errno
import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch as mocked
from diagnostic_fixture import build_relu
from inspect_loadable import Buffer
from test_model_contents import initialize_relu
from verify_relu_loadable import LOADABLE_HASH

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
from loadable import Model, LoadableError


def main():
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    model = Model(data)
    original, addresses, layout = build_relu(data, 0x100000000)
    assert len(model.image) == len(original)-4096
    assert all(m['offset'] == layout[i]['offset']-4096 for i, m in enumerate(model.layout))
    assert [a for a, _ in model.ranges] == [a-0x100000000-4096 for a in addresses]
    assert model.image == original[4096:]
    assert model.inputs[0].name == 'input' and model.outputs[0].name == 'output'
    assert model.inputs[0].shape == (1, 32, 7, 7)
    assert model.inputs[0].format_codes == (3, 2, 2, 36, 0)
    assert model.inputs[0].strides == (2, 224, 1568, 0, 3136, 0, 0, 0)
    with tempfile.TemporaryDirectory(prefix='n1x-loadable-') as directory:
        library = Path(directory)/'contents.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_model.c'), '-o', str(library)], check=True)
        independent = initialize_relu(ctypes.CDLL(str(library)), data, dict(enumerate(model.layout)))
        assert model.image == independent
    inputs = []
    for sign in (-1, 1):
        value = struct.pack('<1568e', *(sign*(0.5+(i%31)/8) for i in range(1568)))
        image, ranges = model.prepare({'input': value})
        assert image[4096:4096+3136] == value and ranges == model.ranges
        assert image[:4096] == model.image[:4096] and image[8192:] == model.image[8192:]
        inputs.append(image)
    assert inputs[0] != inputs[1] and model.image == independent
    view, root = Buffer(data), struct.unpack_from('<I', data)[0]
    tables = {i: view.tables(root, i) for i in range(1, 9)}
    # Valid metadata changes must work without a per-artifact hash allowlist.
    renamed = bytearray(data)
    for table, name in zip(tables[6], (b'image', b'result')):
        pointer = view.field(table, 0)
        target = pointer+view.u('<I', pointer)
        assert len(name) == view.u('<I', target)
        renamed[target+4:target+4+len(name)] = name
    renamed_model = Model(renamed)
    renamed_image, renamed_ranges = renamed_model.prepare({'image': bytes(3136)})
    assert renamed_model.outputs[0].name == 'result' and renamed_ranges == model.ranges
    assert renamed_model.sha256 != model.sha256 and renamed_image == model.image
    rejected = []
    def reject(name, candidate):
        try:
            Model(candidate)
        except LoadableError as exc:
            rejected.append(dict(case=name, reason=str(exc)))
        else:
            raise AssertionError('accepted '+name)
    def patch(name, offset, replacement):
        candidate = bytearray(data)
        candidate[offset:offset+len(replacement)] = replacement
        reject(name, candidate)
    def field(name, table, index, fmt, value):
        at = view.field(table, index)
        assert at is not None, (name, index)
        patch(name, at, struct.pack(fmt, value))
    def absent(name, table, index):
        vt = table-struct.unpack_from('<i', data, table)[0]
        patch(name, vt+4+index*2, b'\0\0')
    patch('identifier', 4, b'NOPE')
    patch('root_outside_file', 0, struct.pack('<I', len(data)))
    patch('invalid_vtable', root, struct.pack('<i', -len(data)))
    vt = root-struct.unpack_from('<i', data, root)[0]
    patch('unknown_root_extension', vt, struct.pack('<H', 26))
    field('serialization_major', root, 0, '<B', 1)
    patch('serialization_minor', view.field(root, 0), bytes((0, 8, 0)))
    field('runtime_version', root, 9, '<B', 4)
    absent('missing_runtime_version', root, 9)
    field('task_interface', tables[1][0], 1, '<I', 2)
    field('task_instance', tables[1][0], 2, '<h', 1)
    event = tables[4][0]
    field('event_value', event, 3, '<I', 2)
    field('event_operation', event, 4, '<B', 0)
    memory = tables[2][3]
    field('unallocated_memory', memory, 2, '<H', 2)
    field('unknown_memory_flags', memory, 2, '<H', 0x13)
    field('memory_zero_size', memory, 3, '<Q', 0)
    field('memory_oversized', memory, 3, '<Q', 1 << 63)
    field('memory_bad_alignment', memory, 4, '<I', 3)
    field('memory_alignment_above_dma_guarantee', memory, 4, '<I', 8192)
    offsets_field = view.field(memory, 6)
    offsets_vector = offsets_field+view.u('<I', offsets_field)
    patch('missing_content_offset', offsets_vector, struct.pack('<I', 0))
    patch('content_outside_memory', offsets_vector+4, struct.pack('<Q', 1 << 63))
    blob = tables[5][-1]
    field('content_size_mismatch', blob, 1, '<Q', 1)
    field('content_interface', blob, 2, '<I', 2)
    field('content_version', blob, 4, '<B', 4)
    field('auxiliary_version', blob, 6, '<B', 4)
    absent('missing_auxiliary_version', blob, 6)
    prop = tables[5][0]
    patch('nonzero_pool', view.vector(prop, 5, 1).start, b'\x01')
    # The last blob's forward name pointer can alias an earlier vector entry's
    # later file string. Duplicate names must fail, independent of table order.
    target = view.field(tables[5][-2], 0)
    target += view.u('<I', target)
    source = view.field(blob, 0)
    assert target > source
    patch('duplicate_content_name', source, struct.pack('<I', target-source))
    field('address_memory_id', tables[3][3], 1, '<H', 999)
    field('address_id', tables[3][3], 0, '<H', 999)
    field('address_size_zero', tables[3][3], 3, '<Q', 0)
    field('address_size_overrun', tables[3][3], 3, '<Q', 1 << 63)
    task_ids = view.vector(tables[1][0], 3, 2)
    patch('task_address_id', task_ids.start, struct.pack('<H', 999))
    tensor = tables[6][0]
    field('tensor_memory_id', tensor, 2, '<H', 999)
    field('tensor_size_overrun', tensor, 3, '<Q', 1 << 63)
    field('tensor_batch', tensor, 10, '<i', 2)
    field('tensor_negative_dimension', tensor, 11, '<i', -1)
    field('tensor_binding', tables[2][2], 8, '<H', 0)
    # A present inline version must fit inside its table object, not merely file.
    vt = blob-view.u('<i', blob)
    size = view.u('<H', vt+2)
    patch('inline_struct_overrun', vt+4+2*6, struct.pack('<H', size-1))
    for length in (0, 7, 8, 64, len(data)-256):
        reject('truncated_'+str(length), data[:length])
    for invalid in ({}, {'unknown': bytes(3136)}, {'input': bytes(3135)}, {'input': bytes(3137)}):
        try:
            model.prepare(invalid)
        except LoadableError:
            pass
        else:
            raise AssertionError('invalid input accepted')
    import run_loadable
    with tempfile.TemporaryDirectory(prefix='n1x-loadable-cli-') as directory:
        base = Path(directory)
        input_file = base/'input.bin'
        input_file.write_bytes(bytes(3136))
        calls = []
        class FakeDevice:
            def __init__(self, path):
                assert path == '/dev/modeled-only'
                calls.append('open')
            def __enter__(self): return self
            def __exit__(self, *_): calls.append('close')
            def info(self): return dict(retain_limit=64 << 20, retained_bytes=0, max_jobs=128, retained_jobs=0)
            def submit(self, image, ranges, writable):
                assert image == model.image and ranges == model.ranges
                assert writable == [(tensor.offset, tensor.bytes)
                                    for tensor in [*model.inputs, *model.outputs]]
                calls.append('submit')
                if fail:
                    error = OSError(errno.ETIMEDOUT, 'modeled failure')
                    error.job_id = 8
                    raise error
                return 8
            def read(self, job_id, offset, size):
                assert (job_id, offset, size) == (8, model.outputs[0].offset, 3136)
                calls.append('read')
                # Deliberately not a ReLU oracle: verify transport/storage only.
                return b'\xa7' * size
        for fail in (False, True):
            destination = base/str(fail)
            argv = ['run_loadable', '--model', str(ROOT/'results/native-compile-t254/relu.nvda'),
                    '--device', '/dev/modeled-only', '--input', 'input='+str(input_file),
                    '--output-dir', str(destination)]
            calls.clear()
            with mocked.object(run_loadable, 'Device', FakeDevice), mocked.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                if fail:
                    try:
                        run_loadable.main()
                    except OSError as exc:
                        assert exc.errno == errno.ETIMEDOUT
                    else:
                        raise AssertionError('execution failure hidden')
                else:
                    run_loadable.main()
            assert (destination/'attempt.json').exists()
            if fail:
                assert calls == ['open', 'submit', 'close']
                error = json.loads((destination/'error.json').read_text())
                assert error['job_id'] == 8 and not error['automatic_retry']
                assert not (destination/'result.json').exists()
            else:
                assert calls == ['open', 'submit', 'read', 'close']
                assert (destination/'000.bin').read_bytes() == b'\xa7' * 3136
                result = json.loads((destination/'result.json').read_text())
                assert result['completed'] and not result['numerical_accuracy_verified']
    print(json.dumps(dict(passed=True, hardware_access=False,
          rejected_cases=len(rejected), cases=rejected, windows_fixture_bytes_match=True,
          native_c_initializer_match=True, changing_inputs_verified=True,
          image_bytes=len(model.image), image_sha256=hashlib.sha256(model.image).hexdigest()), indent=2))


if __name__ == '__main__':
    main()
