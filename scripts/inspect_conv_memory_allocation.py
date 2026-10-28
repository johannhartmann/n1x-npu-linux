#!/usr/bin/env python3
"""Trace original Windows allocation/content initialization for the conv fixture.

Device getter and three provider boundaries are explicit RAM/status models.
The original allocator control flow, property getters and content loader run.
No Windows device, physical DMA or numerical inference is exercised.
"""
import hashlib
import json
import struct
import sys

import pefile
from unicorn import UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_full_deserialization import execute
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH
from inspect_content_materialization import CTX, MODEL
from inspect_loadable import Buffer, inspect
from verify_conv_loadable import LOADABLE_HASH


def probe(u, call, allocate, scalar, vector, data):
    sys.path.insert(0, str(ROOT/'runtime'))
    from loadable import Model
    native = Model(data)
    runtime = allocate(0x1a080, True)
    cache, devices = allocate(32, True), allocate(64, True)
    u.mem_write(runtime, struct.pack('<Q', BASE+0x653d88))
    u.mem_write(runtime+0x80, struct.pack('<Q', cache))
    u.mem_write(runtime+0x68, struct.pack('<Q', devices))
    u.mem_write(runtime+0x98, struct.pack('<I', 3))
    # Zero runtime+0x1a068 selects the original non-cache allocation branch.
    device = allocate(32, True)
    trace, copies, pool_calls, rows = [], [], [], []
    allocations = {}
    failure = None

    def reg(i):
        return u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}'))

    def ret(value):
        u.reg_write(r.UC_ARM64_REG_X0, value)
        u.reg_write(r.UC_ARM64_REG_PC, reg(30))

    def hook(_, pc, size, user):
        a = pc-BASE
        if a in (0x8bb60, 0x8bd30, 0x8b8c0):
            pool_calls.append(hex(a))
        if a == 0x20f40:
            copies.append((reg(0), reg(1), reg(2)))
        if a == 0x25330:
            assert reg(0) == runtime
            ret(device)
        elif a == 0x74798:
            assert reg(0) == device and reg(4) == 0
            length = reg(3)
            assert 0 < length < 4*1024*1024
            trace.append(dict(boundary=hex(a), bytes=length))
            if failure == a:
                ret(6)
                return
            backing, handle = allocate(length+32), allocate(32, True)
            allocations[handle] = (backing, length)
            u.mem_write(reg(1), struct.pack('<Q', handle))
            u.mem_write(reg(2), struct.pack('<Q', backing))
            ret(0)
        elif a == 0x76c58:
            assert reg(0) == device and reg(1) in allocations
            assert reg(2) == 0 and reg(3) == 2
            trace.append(dict(boundary=hex(a)))
            # Opaque provider output, interpreted only by the modeled next call.
            u.mem_write(reg(4), struct.pack('<QQQ', reg(1), 0, 0))
            ret(6 if failure == a else 0)
        elif a == 0x74e78:
            assert reg(0) == device and reg(2) == reg(3) == 1
            assert scalar(reg(1)) in allocations
            trace.append(dict(boundary=hex(a)))
            ret(6 if failure == a else 0)

    hook_handle = u.hook_add(UC_HOOK_CODE, hook)
    properties = {}
    for name, slot, entry in (('local_cvsram', 0x90, 0x8bb60),
                              ('local_dram', 0x98, 0x8bd30),
                              ('global_dram', 0xa0, 0x8b8c0)):
        assert scalar(BASE+0x669260+slot) == BASE+entry
        output = allocate(16)
        assert call(entry, [CTX, output]) == 0
        properties[name] = scalar(output)
        assert bytes(u.mem_read(output+8, 8)) == b'\xa5'*8
    assert properties == dict(local_cvsram=0, local_dram=0, global_dram=4224)
    property_calls_before_allocation = len(pool_calls)

    projected, view = inspect(data), Buffer(data)
    blobs = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        blobs[view.string(table, 0)] = data[extent.start:extent.stop]
    sources = vector(CTX+0xe0, 80)

    def run(name, index, length=None, expected_status=0, copied=None,
            fail_at=None, reuse=False):
        nonlocal failure
        failure = fail_at
        entry, source = projected['memory'][index], sources[index]
        length = entry['size'] if length is None else length
        memory = allocate(128, True)
        u.mem_write(memory+0x18, struct.pack('<Q', length))
        u.mem_write(memory+0x25, bytes([entry['flags']]))
        u.mem_write(memory+0x30, bytes(u.mem_read(source+32, 24)))
        u.mem_write(memory+0x48, bytes(u.mem_read(source+56, 24)))
        before, before_copies = len(trace), len(copies)
        if reuse:
            backing, handle = allocate(length+32), allocate(32, True)
            allocations[handle] = (backing, length)
            u.mem_write(memory, struct.pack('<Q', handle))
            u.mem_write(memory+0x60, struct.pack('<Q', backing))
        status = call(0x1ec70, [runtime, CTX, memory, 0])
        assert status == expected_status, (name, status)
        handle = scalar(memory)
        copied = entry['contents'] if copied is None else copied
        actual = copies[before_copies:]
        assert len(actual) == len(copied), (name, actual, copied)
        if handle:
            backing, allocated = allocations[handle]
            assert allocated == length and scalar(memory+0x60) == backing
            expected = bytearray(b'\xa5'*(length+32))
            for content, (dest, src, count) in zip(copied, actual):
                offset = entry['content_offsets'][entry['contents'].index(content)]
                assert dest == backing+offset and count == len(blobs[content])
                assert bytes(u.mem_read(src, count)) == blobs[content]
                expected[offset:offset+count] = blobs[content]
            assert bytes(u.mem_read(backing, length+32)) == expected, name
            if name.startswith('model_memory_'):
                # Compare all initialized bytes with original executed copies;
                # native unused bytes are zero, Windows RAM-model gaps poisoned.
                native_expected = bytearray(length)
                for dest, src, count in actual:
                    offset = dest-backing
                    native_expected[offset:offset+count] = u.mem_read(src, count)
                memory_start = native.layout[index]['offset']
                assert native.image[memory_start:memory_start+length] == native_expected
        else:
            assert not actual and not scalar(memory+0x60)
        observed = trace[before:]
        if reuse or length > 0xffffffff:
            assert observed == []
        elif fail_at:
            boundaries = [0x74798, 0x76c58, 0x74e78]
            assert [row['boundary'] for row in observed] == [
                hex(a) for a in boundaries[:boundaries.index(fail_at)+1]]
        else:
            assert observed == [dict(boundary='0x74798', bytes=length),
                                dict(boundary='0x76c58'), dict(boundary='0x74e78')]
        rows.append(dict(case=name, status=status, provider_calls=observed,
                         copies=len(actual), copied_bytes=sum(c[2] for c in actual),
                         destination_and_guards_checked=bool(handle)))

    for i in range(len(sources)):
        run('model_memory_'+str(i), i)
    assert rows[1]['provider_calls'][0]['bytes'] == 8192
    assert rows[1]['copied_bytes'] == 2176 and rows[1]['copies'] == 2
    run('constant_exact_extent', 1, 4224)
    run('constant_one_byte_short', 1, 4223, 6, ['tb-1'])
    run('constant_second_offset_at_end', 1, 4096, 6, ['tb-1'])
    run('constant_existing_handle', 1, reuse=True)
    run('size_exceeds_u32', 1, 0x100000000, 11, [])
    for boundary in (0x74798, 0x76c58, 0x74e78):
        run('failure_'+hex(boundary), 1, expected_status=6, copied=[], fail_at=boundary)
    assert len(pool_calls) == property_calls_before_allocation
    assert bytes(u.mem_read(MODEL, len(data))) == data
    u.hook_del(hook_handle)
    return dict(properties=properties, cases=rows, allocator_rva='0x1ec70',
                content_loader_rva='0x20d00', allocation_path_pool_getter_calls=0,
                native_materialization_matches_original_copies=True,
                input_unchanged=True, hardware_access=False,
                limitations=[
                    'Runtime/device/allocator-owned descriptors are RAM fixtures',
                    'Device getter and provider calls 0x74798/0x76c58/0x74e78 are modeled',
                    'Original property getters, allocation decisions and content-copy loop execute',
                    'Optional allocation cache is disabled; full model-load orchestration is not executed',
                    'No claim of DMA allocation, recovery, memory reuse safety or numerical inference'])


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    data = (ROOT/'results/native-compile-t254-conv1x1/conv1x1.nvda').read_bytes()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    report = execute(pefile.PE(str(path)), data, 0xa5, after_deserialize=probe,
                     expected_content_count=10,
                     statistics_layer_prefix='layer-anon-0+layer-anon-0')
    print(json.dumps(dict(passed=True, runtime_sha256=RUNTIME_HASH,
                         loadable_sha256=LOADABLE_HASH, **report['loader']), indent=2))


if __name__ == '__main__':
    main()
