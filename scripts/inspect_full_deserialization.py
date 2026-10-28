#!/usr/bin/env python3
"""Execute original ReLU loadable construction/deserialization without a device.

C++ containers, validators, table extraction, stack guards and regex code
execute original instructions. Required CRT/regex initializers execute under
a bounded single-thread Windows API model. Statistics-index failures are
reported even though the original outer deserializer ignores them.
"""
import hashlib
import json
import struct
from collections import Counter

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH
from inspect_content_materialization import CTX, MODEL, HEAP, STACK, STOP
from inspect_loadable import Buffer, inspect
from verify_relu_loadable import LOADABLE_HASH

STAGES = (0x8deb0, 0x895d0, 0x893e8, 0x88d10, 0x87fc0,
          0x88700, 0x88160, 0x899e8, 0x89208, 0x8c740)
CRT_INITIALIZERS = (0x5f5a0, 0x5f650, 0x56aa0, 0x585f0, 0x4df38)


def execute(pe, data, poison, initialize_regex=True, after_deserialize=None,
            expected_context_version=(3, 0, 0), expected_content_count=8,
            statistics_layer_prefix='layer-anon-0'):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(BASE, (pe.OPTIONAL_HEADER.SizeOfImage + 4095) & ~4095)
    u.mem_write(BASE, pe.get_memory_mapped_image())
    for address, size in ((CTX, 65536), (MODEL, (len(data)+4095)&~4095),
                          (HEAP, 0x1000000), (STACK, 0x100000), (STOP, 4096)):
        u.mem_map(address, size)
    u.mem_write(MODEL, data)
    u.mem_write(STACK, bytes([poison])*0x100000)
    u.mem_write(BASE+0xa5340, struct.pack('<Q', STOP+64))
    imports = {}
    for dll in pe.DIRECTORY_ENTRY_IMPORT:
        for entry in dll.imports:
            target = STOP+0x100+4*len(imports)
            assert target < STOP+4096
            imports[target] = entry.name.decode() if entry.name else str(entry.ordinal)
            u.mem_write(entry.address, struct.pack('<Q', target))
    cursor = HEAP
    stages, regex_calls, stats_returns, stats_missing_regex = [], [], [], []
    stats_headers, initializer_results, optional_libraries = [], [], []
    fls, last_error = {}, 0
    helpers = Counter()
    pattern = pe.get_data(0x98478, 128).split(b'\0')[0]
    assert pattern == rb'^task-?(\d+)-stat(s|_list)$'

    def scalar(address, fmt='<Q'):
        return struct.unpack(fmt, u.mem_read(address, struct.calcsize(fmt)))[0]

    def string(address):
        length, capacity = struct.unpack('<QQ', u.mem_read(address+16, 16))
        assert length <= capacity and length < 4096
        return bytes(u.mem_read(scalar(address) if capacity >= 16 else address, length))

    def reg(i):
        return u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}'))

    def ret(value=None):
        if value is not None:
            u.reg_write(r.UC_ARM64_REG_X0, value & 0xffffffffffffffff)
        u.reg_write(r.UC_ARM64_REG_PC, reg(30))

    def allocate(n, zero=False):
        nonlocal cursor
        n = max(n, 1)
        ptr = cursor
        cursor = (cursor+n+15)&~15
        assert cursor < HEAP+0x1000000
        u.mem_write(ptr, bytes([0 if zero else poison])*n)
        return ptr

    def wide_string(address):
        for n in range(256):
            if scalar(address+2*n, '<H') == 0:
                return bytes(u.mem_read(address, 2*n)).decode('utf-16-le')
        raise AssertionError('Unterminated optional library name')

    def hook(_, pc, size, user):
        nonlocal last_error
        if pc in imports:
            name = imports[pc]
            helpers[name] += 1
            if name in ('EnterCriticalSection', 'LeaveCriticalSection'):
                ret()
            elif name == 'GetProcessHeap':
                ret(1)  # Opaque modeled process-heap handle.
            elif name == 'GetLastError':
                ret(last_error)
            elif name == 'SetLastError':
                last_error = reg(0)&0xffffffff
                ret()
            elif name in ('EncodePointer', 'DecodePointer'):
                ret(reg(0))
            elif name == 'HeapAlloc':
                assert reg(0) == 1 and reg(1) in (0, 8)
                ret(allocate(reg(2), zero=bool(reg(1)&8)))
            elif name == 'HeapFree':
                assert reg(0) == 1 and reg(1) == 0
                ret(1)  # Arena never reuses freed storage.
            elif name == 'FlsAlloc':
                index = len(fls)+1
                fls[index] = 0
                ret(index)
            elif name in ('FlsGetValue', 'FlsSetValue'):
                assert reg(0) in fls
                if name == 'FlsGetValue':
                    last_error = 0
                    ret(fls[reg(0)])
                else:
                    fls[reg(0)] = reg(1)
                    ret(1)
            elif name == 'LoadLibraryExW':
                library = wide_string(reg(0))
                assert library.startswith('api-ms-win-core-') or library == 'kernel32', library
                optional_libraries.append(library)
                last_error = 126  # Optional API-set unavailable; original fallback executes.
                ret(0)
            else:
                raise AssertionError(('Unmodeled Windows API', name))
            return
        a = pc-BASE
        if a in STAGES:
            stages.append(a)
        if a == 0x8ca68:
            stats_missing_regex.append(scalar(BASE+0x6a5b10) == 0)
        if a == 0x88b0c:
            stats_returns.append(reg(0))
        if a == 0x8cf44:
            stats_headers.append(bytes(u.mem_read(reg(25), 12)).hex())
        if a == 0x883ec and initialize_regex:
            assert regex_calls and 'match' not in regex_calls[-1]
            regex_calls[-1]['match'] = bool(reg(0)&255)
        # Execute the real stack guards 0x1000/0x1020: they reserve/release
        # 16 bytes, so replacing them with no-ops can corrupt nested frames.
        if a in (0x4bb68, 0x4c0b0) or pc == STOP+64:
            helpers['free' if a == 0x4bb68 else 'atexit' if a == 0x4c0b0 else 'cfg'] += 1
            ret()
        elif a in (0x6b270, 0x6b290, 0x6b2d8):
            assert reg(0) == CTX+8
            helpers['mutex'] += 1
            ret(0)
        elif a in (0x4bb10, 0x904b0, 0x4c288):
            helpers['allocate'] += 1
            ret(allocate(reg(0)))
        elif a in (0x1100, 0x1820):
            if reg(2):
                value = (bytes(u.mem_read(reg(1), reg(2))) if a == 0x1100
                         else bytes([reg(1)&255])*reg(2))
                u.mem_write(reg(0), value)
            helpers['copy' if a == 0x1100 else 'fill'] += 1
            ret(reg(0))
        elif a == 0x13e0:
            x, y = (bytes(u.mem_read(reg(i), reg(2))) for i in (0, 1))
            helpers['compare'] += 1
            ret((x > y)-(x < y))
        elif a == 0x7d970:
            assert reg(1) == BASE+0x6a5b10
            name = string(reg(0))
            regex_calls.append(dict(name=name.decode('ascii')))
            if not initialize_regex:
                # Retain the old environment as a negative control only.
                import re
                match = re.search(pattern, name, flags=re.ASCII) is not None
                regex_calls[-1]['match'] = match
                ret(int(match))

    u.hook_add(UC_HOOK_CODE, hook)

    def call(entry, args):
        for i, value in enumerate(args):
            u.reg_write(getattr(r, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(r.UC_ARM64_REG_SP, STACK+0xff000)
        u.reg_write(r.UC_ARM64_REG_LR, STOP)
        u.emu_start(BASE+entry, STOP, count=3000000)
        assert u.reg_read(r.UC_ARM64_REG_PC) == STOP, hex(entry)
        assert u.reg_read(r.UC_ARM64_REG_SP) == STACK+0xff000
        return reg(0)

    if initialize_regex:
        for entry in CRT_INITIALIZERS:
            status = call(entry, [])
            assert status == 1
            initializer_results.append(dict(rva=hex(entry), status=status))
        call(0x3490, [])  # Original static regex initializer (void return).
        assert HEAP <= scalar(BASE+0x6a5b10) < cursor
    assert call(0x7e2d8, [CTX]) == CTX
    assert scalar(CTX) == BASE+0x669260
    assert scalar(BASE+0x669328) == BASE+0x888a0
    status = call(0x888a0, [CTX, MODEL, len(data)])
    assert bytes(u.mem_read(MODEL, len(data))) == data

    def vector(address, width):
        begin, end, capacity = struct.unpack('<QQQ', u.mem_read(address, 24))
        assert begin <= end <= capacity and (end-begin) % width == 0
        assert (begin == end == capacity == 0) or HEAP <= begin <= capacity < cursor
        return list(range(begin, end, width))

    def shorts(address):
        return [scalar(a, '<H') for a in vector(address, 2)]

    def nodes(address):
        head, count = struct.unpack('<QQ', u.mem_read(address, 16))
        seen, ordered = set(), []
        assert scalar(head+25, '<B') == 1

        def walk(node):
            if node == head:
                return
            assert node not in seen and len(seen) < count
            seen.add(node)
            assert scalar(node+25, '<B') == 0
            walk(scalar(node))
            ordered.append(node)
            walk(scalar(node+16))

        walk(scalar(head+8))
        assert len(ordered) == count
        return ordered

    report = dict(deserializer_status=status, stages=[hex(a) for a in stages],
                  stats_status=stats_returns, stats_missing_regex=stats_missing_regex,
                  stats_headers=stats_headers, regex_initialized=initialize_regex,
                  crt_initializers=initializer_results, optional_libraries=optional_libraries,
                  heap_poison=poison, helpers=dict(helpers), input_unchanged=True)
    if status:
        assert stages == [0x8deb0] and not stats_returns and not regex_calls
        return report

    assert stages == list(STAGES)
    stats_nodes = nodes(CTX+0x1b0)
    assert scalar(CTX+0x1c0, '<I') == 0
    context_version = bytes(u.mem_read(CTX+0x1a8, 3))
    assert context_version == bytes(expected_context_version), (list(context_version), list(expected_context_version))
    report["context_version"] = list(context_version)
    projected, view = inspect(data), Buffer(data)
    root = view.u('<I', 0)
    tasks = [dict(id=scalar(a, '<H'), interface=scalar(a+4, '<I'),
                  instance=scalar(a+8, '<h'), pre_actions=shorts(a+16),
                  post_actions=shorts(a+40), address_ids=shorts(a+64))
             for a in vector(CTX+0xf8, 88)]
    submits = [dict(id=scalar(a, '<H'), task_ids=shorts(a+8))
               for a in vector(CTX+0x110, 32)]
    addresses = [dict(id=scalar(a, '<H'), memory_id=scalar(a+2, '<H'),
                      size=scalar(a+8), offset=scalar(a+16))
                 for a in vector(CTX+0x140, 24)]
    memories = []
    for a in vector(CTX+0xe0, 80):
        names = [string(s).decode() for s in vector(a+32, 32)]
        memories.append(dict(id=scalar(a, '<H'), size=scalar(a+8),
            alignment=scalar(a+16, '<I'), domain=scalar(a+20, '<B'),
            flags=scalar(a+21, '<B'), contents=names, content_count=len(names),
            content_offsets=[scalar(p) for p in vector(a+56, 8)]))
    assert tasks == projected['tasks'] and submits == projected['submits']
    assert addresses == projected['addresses'] and memories == projected['memory']
    content_nodes = nodes(CTX+0xd0)
    assert [string(n+32) for n in content_nodes] == sorted(string(n+32) for n in content_nodes)
    by_name = {view.string(t, 0): t for t in view.tables(root, 5)}
    assert len(content_nodes) == len(by_name) == expected_content_count
    content_extensions = {}
    for n in content_nodes:
        name = string(n+32).decode()
        assert string(n+64).decode() == name
        t = by_name[name]
        extent = view.vector(t, 5, 1)
        payload = data[extent.start:extent.stop]
        assert scalar(n+112) == len(payload) == view.scalar(t, 1, '<Q')
        assert bytes(u.mem_read(scalar(n+120), len(payload))) == payload
        for field, offset in ((2, 96), (3, 100)):
            assert scalar(n+offset, '<I') == view.scalar(t, field, '<I')
        f = view.field(t, 4)
        assert bytes(u.mem_read(n+104, 3)) == data[f:f+3]
        auxiliary = view.field(t, 6)
        expected_aux = data[auxiliary:auxiliary+3] if auxiliary is not None else bytes((3, 13, 0))
        actual_aux = bytes(u.mem_read(n+107, 3))
        assert actual_aux == expected_aux
        content_extensions[name] = list(actual_aux)
    report['content_auxiliary_versions'] = content_extensions
    assert [e['name'] for e in regex_calls if e['match']] == ['task-0-stats']
    assert len(regex_calls) == expected_content_count
    stats_table = by_name['task-0-stats']
    extent = view.vector(stats_table, 5, 1)
    stats = data[extent.start:extent.stop]
    assert stats[:4] == b'\0\3\0\0' and stats[4] in (1, 2)
    assert len(stats) == 28736
    header = {hex(offset): struct.unpack_from('<I', stats, offset)[0]
              for offset in (8, 12, 16, 20, 24)}
    assert header == {'0x8': 4, '0xc': 6144, '0x10': 28, '0x14': 1024, '0x18': 24640}
    name_offsets = [28+6144*i for i in range(4)]
    for i, offset in enumerate(name_offsets):
        assert stats[offset:offset+6144].split(b'\0')[0] == (
            f'{statistics_layer_prefix}_cpipe_tile_{i}_ts_hwl_cpipe_{i}'.encode())
    report['statistics_header_words'] = header
    report['statistics_layer_name_offsets'] = name_offsets
    report['actual_statistics_bytes'] = len(stats)
    if context_version != bytes((3, 0, 0)):
        assert stats_returns == [2] and stats_missing_regex == [False] and not stats_headers and not stats_nodes
    elif not initialize_regex:
        assert stats_returns == [8] and stats_missing_regex == [True] and not stats_headers and not stats_nodes
    else:
        assert stats_missing_regex == [False] and stats_headers == [stats[:12].hex()], (list(context_version), stats_returns, stats_missing_regex, stats_headers, len(stats_nodes))
        if stats[4] == 2:
            assert stats_returns == [2] and not stats_nodes
        else:
            assert stats_returns == [0] and len(stats_nodes) == 1
            node = stats_nodes[0]
            assert scalar(node+32, '<H') == 0
            count = struct.unpack_from('<I', stats, 8)[0]
            old_extent = (count*0x340+0x4b)&~0x3f
            assert scalar(node+112) == old_extent == 3392
            assert bytes(u.mem_read(scalar(node+120), len(stats))) == stats
            report['old_statistics_extent'] = old_extent
            report['actual_statistics_bytes'] = len(stats)
    events = vector(CTX+0x128, 12)
    assert len(events) == 1 and bytes(u.mem_read(events[0], 12)) == bytes.fromhex('000000000100000001000000')
    assert not vector(CTX+0x170, 32)  # No relocation records in this fixture.
    tensors = vector(CTX+0x158, 112)
    assert len(tensors) == 2
    for a, t in zip(tensors, view.tables(root, 6)):
        assert string(a).decode() == view.string(t, 0)
        for field, offset, fmt in ((1, 32, '<H'), (2, 34, '<H'), (3, 40, '<Q'), (4, 48, '<Q')):
            assert scalar(a+offset, fmt) == view.scalar(t, field, fmt)
        for field in range(5, 10):
            assert scalar(a+72+field-5, '<B') == view.scalar(t, field, '<B')
        for field in range(10, 14):
            assert scalar(a+56+4*(field-10), '<I') == view.scalar(t, field, '<I')
        for field in range(14, 22):
            assert scalar(a+80+4*(field-14), '<I') == view.scalar(t, field, '<I')
    report.update(tasks=tasks, submits=submits, memory_records=len(memories),
                  address_records=len(addresses), content_records=len(content_nodes),
                  tensor_records=len(tensors), event_records=len(events), relocation_records=0)
    if after_deserialize is not None:
        report['loader'] = after_deserialize(u, call, allocate, scalar, vector, data)
    return report


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(path))
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    rows = [dict(case='original_relu', **execute(pe, data, poison)) for poison in (0, 0xa5)]
    rows.append(dict(case='missing_regex_initialization', **execute(pe, data, 0xa5, False)))
    view = Buffer(data)
    root = view.u('<I', 0)
    memory_ref = bytearray(data)
    struct.pack_into('<H', memory_ref, view.field(view.tables(root, 3)[3], 1), 99)
    task_ref = bytearray(data)
    struct.pack_into('<H', task_ref, next(iter(view.vector(view.tables(root, 1)[0], 3, 2))), 99)
    blob_ref = bytearray(data)
    p = next(iter(view.vector(view.tables(root, 2)[3], 5)))
    blob_ref[p+view.u('<I', p)+4] = ord('X')
    version_only = bytearray(data)
    stats_table = next(t for t in view.tables(root, 5) if view.string(t, 0) == 'task-0-stats')
    version_only[view.vector(stats_table, 5, 1).start+4] = 1
    for name, candidate, status in (('invalid_memory_reference', memory_ref, 0x30007),
                                     ('invalid_task_address_reference', task_ref, 0),
                                     ('missing_content_reference', blob_ref, 0),
                                     ('unsafe_statistics_version_only_mutation', version_only, 0)):
        row = execute(pe, bytes(candidate), 0xa5)
        assert row['deserializer_status'] == status
        rows.append(dict(case=name, **row))
    print(json.dumps(dict(passed=True, hardware_access=False, loadable_sha256=LOADABLE_HASH,
        constructor_rva='0x7e2d8', deserializer_rva='0x888a0', cases=rows,
        limitations=[
            'Only required CRT and regex initializers execute, not complete DLL attachment or device initialization',
            'Original statistics header check rejects version byte 2 with status 2; outer return 0 ignores it',
            'Windows APIs use a single-thread arena/FLS model and unavailable optional API-set lookups; original C-locale fallback executes',
            'Allocations, byte helpers, mutexes, atexit registration and CFG are modeled',
            'C++ containers, table materializers, reference validators and regex compiler/matcher execute original code',
            'The missing-initializer control alone retains a modeled ASCII regex predicate and reports status 8',
            'Version-only mutation makes the old runtime index 3392 of 28736 bytes; this is a diagnostic counterexample, not a fix',
            'The two accepted malformed references show deserialization success is not full model validity',
            'Only the pinned ReLU model and explicit in-memory mutations are qualified; no hardware compatibility claim'
        ]), indent=2))


if __name__ == '__main__':
    main()
