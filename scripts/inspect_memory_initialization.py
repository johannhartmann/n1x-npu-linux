#!/usr/bin/env python3
"""Execute the complete Windows memory-content loader after deserialization.

The allocator-owned destination descriptor and runtime provider context are
explicit RAM fixtures. No device allocation, complete model load or DMA runs.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_full_deserialization import execute
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH
from inspect_content_materialization import CTX, MODEL
from inspect_loadable import Buffer, inspect
from verify_relu_loadable import LOADABLE_HASH


def probe(u, call, allocate, scalar, vector, data):
    runtime = allocate(0x300, True)
    cache, devices = allocate(32, True), allocate(64, True)
    u.mem_write(runtime, struct.pack('<Q', BASE+0x653d88))
    u.mem_write(runtime+0x80, struct.pack('<Q', cache))
    u.mem_write(runtime+0x68, struct.pack('<Q', devices))
    u.mem_write(runtime+0x98, struct.pack('<I', 3))
    assert scalar(BASE+0x669260+0xc0) == BASE+0x8b350
    copies, callbacks, getters, versions = [], [], [], []

    def observe(_, pc, size, user):
        a = pc-BASE
        if a == 0x20f40:
            copies.append([u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}')) for i in range(3)])
        elif a == 0x1b280:
            callbacks.append(True)
        elif a == 0x8b350:
            getters.append(True)
        elif a in (0x25410, 0x25460, 0x25630, 0x75810, 0x2b230):
            versions.append(hex(a))

    handle = u.hook_add(UC_HOOK_CODE, observe)
    view, projected = Buffer(data), inspect(data)
    blobs = {}
    for t in view.tables(view.u('<I', 0), 5):
        extent = view.vector(t, 5, 1)
        blobs[view.string(t, 0)] = data[extent.start:extent.stop]
    rows = []

    def strings(names):
        start = allocate(max(32*len(names), 1), True)
        for i, name in enumerate(names):
            value, slot = name.encode(), start+32*i
            if len(value) < 16:
                u.mem_write(slot, value+b'\0')
                capacity = 15
            else:
                backing = allocate(len(value)+1, True)
                u.mem_write(backing, value+b'\0')
                u.mem_write(slot, struct.pack('<Q', backing))
                capacity = len(value)
            u.mem_write(slot+16, struct.pack('<QQ', len(value), capacity))
        return struct.pack('<QQQ', start, start+32*len(names), start+32*len(names))

    def run(name, names, offsets, size, flags=3, expected_status=0,
            copied=None, source=None, null_model=False, null_memory=False):
        destination = allocate(size+32)
        initial = b'\xa5'*(size+32)
        u.mem_write(destination, initial)
        memory = allocate(0x80, True)
        u.mem_write(memory, struct.pack('<Q', runtime))
        u.mem_write(memory+0x18, struct.pack('<Q', size))
        u.mem_write(memory+0x25, bytes([flags]))
        if source is not None:
            # Reuse the actual deserializer's name/offset vectors unchanged.
            u.mem_write(memory+0x30, bytes(u.mem_read(source+32, 24)))
            u.mem_write(memory+0x48, bytes(u.mem_read(source+56, 24)))
        else:
            u.mem_write(memory+0x30, strings(names))
            ptr = allocate(max(len(offsets)*8, 1), True)
            if offsets:
                u.mem_write(ptr, struct.pack('<'+'Q'*len(offsets), *offsets))
            u.mem_write(memory+0x48, struct.pack('<QQQ', ptr, ptr+8*len(offsets), ptr+8*len(offsets)))
        u.mem_write(memory+0x60, struct.pack('<Q', destination))
        descriptor = bytes(u.mem_read(memory, 0x80))
        before = len(copies), len(callbacks), len(getters), len(versions)
        status = call(0x20d00, [runtime, 0 if null_model else CTX, 0 if null_memory else memory])
        assert status == expected_status, (name, hex(status))
        if copied is None:
            copied = list(zip(names, offsets)) if flags & 2 and not status else []
        expected = bytearray(initial)
        for content, offset in copied:
            expected[offset:offset+len(blobs[content])] = blobs[content]
        assert len(expected) == len(initial)
        assert bytes(u.mem_read(destination, size+32)) == bytes(expected), name
        assert bytes(u.mem_read(memory, 0x80)) == descriptor
        actual = copies[before[0]:]
        assert len(actual) == len(callbacks)-before[1] == len(copied)
        for (dest, src, length), (content, offset) in zip(actual, copied):
            assert dest == destination+offset and length == len(blobs[content])
            assert bytes(u.mem_read(src, length)) == blobs[content]
        rows.append(dict(case=name, status=status, content_lookups=len(getters)-before[2],
                         copies=len(actual), bytes_copied=sum(c[2] for c in actual),
                         version_calls=versions[before[3]:],
                         guard_unchanged=True, destination_matches_expected=True))

    memories = vector(CTX+0xe0, 80)
    for source, entry in zip(memories, projected['memory']):
        run('model_memory_'+str(entry['id']), entry['contents'], entry['content_offsets'],
            entry['size'], entry['flags'], source=source)
    assert sum(row['copies'] for row in rows) == 5
    assert sum(row['bytes_copied'] for row in rows) == 2798720
    assert versions.count('0x75810') == 1  # Original provider populates cache once.
    assert versions.count('0x25410') == versions.count('0x2b230') == 5
    nw, ctrl = 'task-0-nw', 'task-0-ctrl_data'
    run('missing_name', ['Xask-0-nw'], [0], 256, expected_status=11)
    run('offset_count_mismatch', [nw], [], 256, expected_status=4)
    run('extra_offset', [nw], [0, 16], 256, expected_status=4)
    run('short_destination', [nw], [0], 255, expected_status=6)
    run('offset_overrun', [nw], [1], 256, expected_status=6)
    run('nonzero_offset_exact_end', [nw], [64], 320)
    run('empty_initialized_memory', [], [], 64)
    run('initialization_flag_clear', ['missing'], [], 64, flags=1)
    run('null_model', [], [], 64, expected_status=4, null_model=True)
    run('null_memory', [], [], 64, expected_status=4, null_memory=True)
    run('two_contents', [nw, ctrl], [32, 320], 640)
    run('later_missing_name', [nw, 'missing'], [32, 320], 640,
        expected_status=11, copied=[(nw, 32)])
    run('later_overrun', [nw, ctrl], [32, 321], 640,
        expected_status=6, copied=[(nw, 32)])
    # The original loader permits overlapping destinations and copies in order.
    run('overlapping_contents', [nw, ctrl], [32, 64], 384)
    assert bytes(u.mem_read(MODEL, len(data))) == data
    assert bytes(u.mem_read(cache, 3)) == bytes([3, 0, 0])
    u.hook_del(handle)
    return dict(entry_rva='0x20d00', cases=rows,
                input_unchanged=True, runtime_cache=list(u.mem_read(cache, 3)),
                hardware_access=False,
                limitations=[
                    'Allocator-owned memory descriptors and provider/device/cache context are RAM fixtures',
                    'Original getter, strings/maps, version providers, complete loader loop and callback execute',
                    'CRT, allocation, byte helpers, mutex and CFG use the full-deserializer probe models',
                    'This is memory-content initialization, not device allocation or complete model loading',
                    'Earlier successful copies persist when a later lookup or extent check fails',
                    'No DMA, firmware boot or numerical inference is performed'])


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    report = execute(pefile.PE(str(path)), data, 0xa5, after_deserialize=probe)
    assert report['deserializer_status'] == 0 and report['stats_status'] == [2]
    print(json.dumps(dict(passed=True, loadable_sha256=LOADABLE_HASH,
                         runtime_sha256=RUNTIME_HASH, **report['loader']), indent=2))


if __name__ == '__main__':
    main()
