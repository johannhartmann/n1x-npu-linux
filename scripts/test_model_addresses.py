#!/usr/bin/env python3
"""Validate model-to-task address resolution before the firmware wire format."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from diagnostic_fixture import build_relu
from inspect_loadable import inspect
from inspect_dla_compiler import ROOT
from verify_relu_loadable import LOADABLE_HASH


class Range(ctypes.Structure):
    _fields_ = [('address', ctypes.c_uint64), ('bytes', ctypes.c_uint64)]


class Memory(ctypes.Structure):
    _fields_ = [('mapping', Range), ('logical_bytes', ctypes.c_uint64)]


class Address(ctypes.Structure):
    _fields_ = [('offset', ctypes.c_uint64), ('bytes', ctypes.c_uint64),
                ('memory_index', ctypes.c_uint16)]


class TaskBuffer(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('address', 'logical_bytes', 'mapped_bytes')]


def resolver(lib):
    function = lib.n1x_dla_resolve_task_buffers
    function.argtypes = [ctypes.POINTER(TaskBuffer), ctypes.c_size_t,
        ctypes.POINTER(Memory), ctypes.c_uint16, ctypes.POINTER(Address), ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint16]
    function.restype = ctypes.c_int
    return function


def resolve_relu(lib, data, layout):
    """Use trusted RAM backing extents and the pinned model's checked ID arrays."""
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    model = inspect(data)
    assert [m['id'] for m in model['memory']] == list(range(len(model['memory'])))
    assert [a['id'] for a in model['addresses']] == list(range(len(model['addresses'])))
    memories = (Memory*len(model['memory']))(*[
        Memory(Range(layout[m['id']]['address'], layout[m['id']]['backing']), m['size'])
        for m in model['memory']])
    addresses = (Address*len(model['addresses']))(*[
        Address(a['offset'], a['size'], a['memory_id']) for a in model['addresses']])
    ids = model['tasks'][0]['address_ids']
    indices = (ctypes.c_uint16*len(ids))(*ids)
    output = (TaskBuffer*len(ids))()
    assert resolver(lib)(output, len(output), memories, len(memories),
                         addresses, len(addresses), indices, len(indices)) == 0
    for i, index in enumerate(ids):
        entry = model['addresses'][index]
        memory = layout[entry['memory_id']]
        assert (output[i].address, output[i].logical_bytes, output[i].mapped_bytes) == (
            memory['address']+entry['offset'], entry['size'], memory['backing']-entry['offset'])
    return output


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-model-addresses-') as tmp:
        library = Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_task.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    resolve = resolver(lib)
    cases = []

    def check(name, mutate=None, *, valid=False, counts=(2, 3, 4), capacity=4, null=None):
        memories = (Memory*2)(Memory(Range(0x100000, 1280), 1024), Memory(Range(0x200000, 256), 64))
        addresses = (Address*3)(Address(256, 256, 0), Address(0, 64, 1), Address(0, 1024, 0))
        indices = (ctypes.c_uint16*4)(2, 1, 0, 1)
        if mutate:
            mutate(memories, addresses, indices)
        before = [bytes(v) for v in (memories, addresses, indices)]
        output = (TaskBuffer*5)()
        ctypes.memset(output, 0xa5, ctypes.sizeof(output))
        result = resolve(None if null == 'out' else output, capacity,
            None if null == 'memories' else memories, counts[0],
            None if null == 'addresses' else addresses, counts[1],
            None if null == 'indices' else indices, counts[2])
        assert result == (0 if valid else -errno.EINVAL), name
        assert [bytes(v) for v in (memories, addresses, indices)] == before
        if valid:
            expected = []
            for index in indices:
                a = addresses[index]
                m = memories[a.memory_index]
                expected.append((m.mapping.address+a.offset, a.bytes, m.mapping.bytes-a.offset))
            assert [(o.address, o.logical_bytes, o.mapped_bytes) for o in output[:4]] == expected
            assert bytes(output[4]) == b'\xa5'*24
        else:
            assert bytes(output) == b'\xa5'*ctypes.sizeof(output)
        cases.append(dict(case=name, accepted=valid, output_unchanged_on_error=not valid))

    check('reordered_aliases_and_padded_subranges', valid=True)
    for name, mutate in (
        ('task_index_equal_count', lambda m, a, t: t.__setitem__(3, 3)),
        ('task_index_outside_table', lambda m, a, t: t.__setitem__(3, 65535)),
        ('memory_index_equal_count', lambda m, a, t: setattr(a[1], 'memory_index', 2)),
        ('null_mapping', lambda m, a, t: setattr(m[1].mapping, 'address', 0)),
        ('logical_size_exceeds_mapping', lambda m, a, t: setattr(m[1], 'logical_bytes', 257)),
        ('empty_memory', lambda m, a, t: setattr(m[1], 'logical_bytes', 0)),
        ('empty_address', lambda m, a, t: setattr(a[1], 'bytes', 0)),
        ('offset_at_logical_end', lambda m, a, t: setattr(a[1], 'offset', 64)),
        ('extent_exceeds_logical_not_mapping', lambda m, a, t: setattr(a[1], 'bytes', 65)),
        ('offset_overflow', lambda m, a, t: setattr(a[1], 'offset', (1 << 64)-1)),
        ('size_overflow', lambda m, a, t: setattr(a[1], 'bytes', (1 << 64)-1)),
        ('mapping_address_overflow', lambda m, a, t: setattr(m[1].mapping, 'address', (1 << 64)-128)),
        ('mapping_size_overflow', lambda m, a, t: setattr(m[1].mapping, 'bytes', (1 << 64)-1)),
    ):
        check(name, mutate)
    for counts in ((0, 3, 4), (2, 0, 4), (2, 3, 0), (2, 3, 513)):
        check(f'invalid_counts_{counts}', counts=counts)
    check('short_output', capacity=3)
    for null in ('out', 'memories', 'addresses', 'indices'):
        check('null_'+null, null=null)
    memories = (Memory*1)(Memory(Range(0x300000, 256), 64))
    addresses = (Address*1)(Address(0, 64, 0))
    indices = (ctypes.c_uint16*512)()
    output = (TaskBuffer*512)()
    assert resolve(output, 512, memories, 1, addresses, 1, indices, 512) == 0
    assert all((b.address, b.logical_bytes, b.mapped_bytes) == (0x300000, 64, 256) for b in output)
    cases.append(dict(case='maximum_cached_list', accepted=True))
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    _, expected, layout = build_relu(data, 0x20001000)
    resolved = resolve_relu(lib, data, layout)
    assert [b.address for b in resolved] == expected
    print(json.dumps(dict(passed=True, hardware_access=False, cases=cases,
        relu_task_indices=[3, 1, 2, 4, 5, 6, 7, 8],
        relu_buffers=[{name: getattr(b, name) for name, _ in TaskBuffer._fields_} for b in resolved],
        limitations=['Trusted RAM allocation extents; no physical DMA mapping',
                     'Dense IDs are checked by the caller; selected references only',
                     'Logical extents and accessible mapping bytes remain separate',
                     'No instruction, model-wide content, alignment or hardware-address-width validation']), indent=2))


if __name__ == '__main__':
    main()
