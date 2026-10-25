#!/usr/bin/env python3
"""Check native named-content preparation, including all-or-nothing validation."""
import ctypes as c
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from diagnostic_fixture import build_relu
from inspect_loadable import Buffer, inspect
from inspect_dla_compiler import ROOT
from verify_relu_loadable import LOADABLE_HASH


class Memory(c.Structure):
    _fields_ = [('data', c.c_void_p), ('mapped_bytes', c.c_size_t), ('logical_bytes', c.c_uint64)]


class Content(c.Structure):
    _fields_ = [('name', c.c_void_p), ('name_bytes', c.c_size_t),
                ('data', c.c_void_p), ('bytes', c.c_size_t)]


class Init(c.Structure):
    _fields_ = [('name', c.c_void_p), ('name_bytes', c.c_size_t),
                ('offset', c.c_uint64), ('memory_index', c.c_uint16)]


def initializer(lib):
    function = lib.n1x_dla_initialize_contents
    function.argtypes = [c.POINTER(Memory), c.c_size_t, c.POINTER(Content),
                         c.c_size_t, c.POINTER(Init), c.c_size_t]
    function.restype = c.c_int
    return function


def initialize_relu(lib, data, layout):
    """Project the pinned model into native views of privately owned RAM.

    Allocation extents are from the fixture allocator; the C helper receives
    names as names, not Python-resolved payload pointers for each initializer.
    """
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    model, view = inspect(data), Buffer(data)
    assert [m['id'] for m in model['memory']] == list(range(len(model['memory'])))
    payload = bytearray(max(m['offset']+m['backing'] for m in layout.values()))
    backing = (c.c_ubyte*len(payload)).from_buffer(payload)
    memories = (Memory*len(model['memory']))(*[
        Memory(c.addressof(backing)+layout[m['id']]['offset'], layout[m['id']]['backing'], m['size'])
        for m in model['memory']])
    owned = []

    def storage(value):
        buf = c.create_string_buffer(value, max(len(value), 1))
        owned.append(buf)
        return c.addressof(buf)

    contents, initializers = [], []
    for table in view.tables(view.u('<I', 0), 5):
        name = view.string(table, 0).encode()
        extent = view.vector(table, 5, 1)
        blob = data[extent.start:extent.stop]
        assert view.scalar(table, 1, '<Q') == len(blob)
        contents.append(Content(storage(name), len(name), storage(blob), len(blob)))
    for memory in model['memory']:
        assert len(memory['contents']) == len(memory['content_offsets'])
        if memory['flags'] & 2:
            for name, offset in zip(memory['contents'], memory['content_offsets']):
                value = name.encode()
                initializers.append(Init(storage(value), len(value), offset, memory['id']))
    catalog = (Content*len(contents))(*contents)
    entries = (Init*len(initializers))(*initializers)
    source_before = [bytes(b) for b in owned]
    assert initializer(lib)(memories, len(memories), catalog, len(catalog), entries, len(entries)) == 0
    assert [bytes(b) for b in owned] == source_before
    assert len(initializers) == 5
    return payload


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-model-contents-') as tmp:
        library = Path(tmp)/'model.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_model.c'), '-o', str(library)], check=True)
        lib = c.CDLL(str(library))
    initialize = initializer(lib)
    cases = []

    def check(name, *, names=(b'first', b'second'), data=(b'ABCD', b'uvwxyz'),
              entries=((0, b'first', 8), (1, b'second', 12)),
              logical=(32, 32), valid=False, mutate=None, null=None):
        buffers = [c.create_string_buffer(b'\xa5'*80, 80) for _ in range(2)]
        # 16-byte prefix/suffix guards, 48 mapped bytes, 32 logical by default.
        memories = (Memory*2)(*[Memory(c.addressof(b)+16, 48, n) for b, n in zip(buffers, logical)])
        owned = []

        def store(value):
            # Deliberately no terminating NUL: name comparisons must be bounded.
            buf = c.create_string_buffer(value, max(len(value), 1))
            owned.append(buf)
            return c.addressof(buf)

        contents = (Content*len(names))(*[
            Content(store(n), len(n), store(d), len(d)) for n, d in zip(names, data)])
        initializers = (Init*len(entries))(*[
            Init(store(n), len(n), offset, memory) for memory, n, offset in entries])
        if mutate:
            mutate(memories, contents, initializers)
        source_before = [bytes(a) for a in (memories, contents, initializers, *owned)]
        status = initialize(None if null == 'memories' else memories, len(memories),
            None if null == 'contents' else contents, len(contents),
            None if null == 'initializers' else initializers, len(initializers))
        assert status == (0 if valid else -errno.EINVAL), (name, status)
        expected = [bytearray(b'\xa5'*80) for _ in buffers]
        if valid:
            blobs = dict(zip(names, data))
            for memory, content, offset in entries:
                value = blobs[content]
                expected[memory][16+offset:16+offset+len(value)] = value
        assert [bytes(b) for b in buffers] == [bytes(b) for b in expected], name
        assert [bytes(a) for a in (memories, contents, initializers, *owned)] == source_before
        cases.append(dict(case=name, accepted=valid, buffers_match_expected=True,
                          input_unchanged=True, all_buffers_unchanged_on_error=not valid))

    check('two_memories', valid=True)
    check('nonzero_offset_exact_end', entries=((0, b'first', 28),), valid=True)
    check('overlapping_destinations_in_order', entries=((0, b'first', 8), (0, b'second', 10)), valid=True)
    check('repeated_name', entries=((0, b'first', 8), (1, b'first', 12)), valid=True)
    check('binary_names', names=(b'a\0b', b'a\0c'),
          entries=((0, b'a\0b', 8), (1, b'a\0c', 12)), valid=True)
    check('empty_contents_at_logical_end', data=(b'', b'uvwxyz'),
          entries=((0, b'first', 32),), valid=True,
          mutate=lambda m, b, i: setattr(b[0], 'data', None))
    check('no_initializers', entries=(), valid=True)
    check('missing_first_name', entries=((0, b'missing', 8),))
    check('missing_later_name', entries=((0, b'first', 8), (1, b'missing', 12)))
    check('prefix_is_not_equal', entries=((0, b'firs', 8),))
    check('duplicate_selected_name', names=(b'first', b'first'), entries=((0, b'first', 8),))
    check('late_logical_overrun_despite_padding', entries=((0, b'first', 8), (1, b'second', 27)))
    check('late_offset_overflow', entries=((0, b'first', 8), (1, b'second', (1 << 64)-1)))
    check('late_memory_index_equal_count', entries=((0, b'first', 8), (2, b'second', 12)))
    check('late_memory_index_outside_table', entries=((0, b'first', 8), (65535, b'second', 12)))
    check('logical_exceeds_backing', logical=(32, 49))
    check('empty_logical_memory', logical=(32, 0))
    check('logical_size_overflow', logical=(32, (1 << 64)-1))
    for name, mutate in (
        ('null_later_destination', lambda m, b, i: setattr(m[1], 'data', None)),
        ('null_later_source', lambda m, b, i: setattr(b[1], 'data', None)),
        ('null_catalog_name', lambda m, b, i: setattr(b[1], 'name', None)),
        ('empty_catalog_name', lambda m, b, i: setattr(b[1], 'name_bytes', 0)),
        ('null_later_reference_name', lambda m, b, i: setattr(i[1], 'name', None)),
        ('empty_later_reference_name', lambda m, b, i: setattr(i[1], 'name_bytes', 0)),
        ('later_source_extent_overflow', lambda m, b, i: setattr(b[1], 'bytes', c.c_size_t(-1).value)),
    ):
        check(name, mutate=mutate)
    for null in ('memories', 'contents', 'initializers'):
        check('null_'+null, null=null)
    assert initialize(None, 0, None, 0, None, 0) == 0
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    reference, _, layout = build_relu(data, 0x20001000)
    native = initialize_relu(lib, data, layout)
    assert native == reference
    print(json.dumps(dict(passed=True, hardware_access=False, cases=cases,
        empty_tables_accepted=True, loadable_sha256=LOADABLE_HASH,
        relu_payload_matches_independent_fixture=True, relu_initialized_bytes=2798720,
        limitations=['Caller provides bounded immutable views and trusted private CPU allocations',
                     'No general FlatBuffers parser or content-version validation',
                     'Source/destination aliasing is forbidden by the caller contract',
                     'No DMA synchronization, hardware submission or numerical inference']), indent=2))


if __name__ == '__main__':
    main()
