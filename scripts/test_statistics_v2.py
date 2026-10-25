#!/usr/bin/env python3
"""Check the Linux statistics-layout parser against original Windows production.

The compiler builder runs in Unicorn with only allocation, fill and copy
modeled. Metadata is opaque input; this does not emulate counter generation.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT

COMPILER_HASH = '0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
STATISTICS_HASH = 'da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3'
CTX, GRAPH, HEAP, STACK, STOP = 0x40000000, 0x40010000, 0x41000000, 0x50000000, 0x51000000


class Layout(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('count', 'metadata_offset', 'metadata_stride', 'data_offset', 'data_stride')]


def produce(pe, records, *, extra_active=0, data_bytes=None, flag=0,
            disable=False, wrong_reservation=False, distributed=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    u.mem_map(base, (len(image)+4095)&~4095)
    u.mem_write(base, image)
    for address, size in ((CTX, 0x200000), (HEAP, 0x100000), (STACK, 0x10000), (STOP, 4096)):
        u.mem_map(address, size)
    u.mem_write(STACK, b'\xa5'*0x10000)
    count = len(records)
    assert count <= 32 and extra_active <= 32 and flag in (0, 1, 2)
    active = count+extra_active
    offset = (28+active*6144+63)&~63
    data_bytes = 1024*count if data_bytes is None else data_bytes
    total = offset+data_bytes
    u.mem_write(CTX+0x68, struct.pack('<Q', GRAPH))
    if not disable:
        u.mem_write(GRAPH+0x3d+flag, b'\1')
    u.mem_write(GRAPH, struct.pack('<QQ', GRAPH+0x1000, GRAPH+0x1000+8*active))
    u.mem_write(CTX+0x1788, struct.pack('<I', offset+(64 if wrong_reservation else 0)))
    u.mem_write(CTX+0x700, struct.pack('<I', data_bytes))
    for i in range(active):
        obj = GRAPH+0x2000+16*i
        u.mem_write(GRAPH+0x1000+8*i, struct.pack('<Q', obj))
        u.mem_write(obj+4, struct.pack('<I', 1))
    groups = [[] for _ in range(13)]
    for i, record in enumerate(records):
        assert len(record) == 6144
        address = GRAPH+0x10000+i*6144
        u.mem_write(address, record)
        groups[i % 13 if distributed else 0].append((address, record))
    ordered = []
    for i, group in enumerate(groups):
        vector = GRAPH+0x3000+i*256
        end = vector+8*len(group)
        u.mem_write(CTX+0x738+24*i, struct.pack('<QQQ', vector, end, end))
        for j, (address, record) in enumerate(group):
            u.mem_write(vector+8*j, struct.pack('<Q', address))
            ordered.append((address, record))
    trace = []

    def hook(_, pc, size, user):
        x0, x1, x2 = (u.reg_read(reg) for reg in
                      (r.UC_ARM64_REG_X0, r.UC_ARM64_REG_X1, r.UC_ARM64_REG_X2))
        if pc == base+0xf03068:
            assert x0 == total and not trace and 0 < x0 <= 0x100000
            u.mem_write(HEAP, b'\xa5'*x0)
            trace.append(dict(operation='allocate', bytes=x0))
            u.reg_write(r.UC_ARM64_REG_X0, HEAP)
        elif pc == base+0x14e0:
            assert (x0, x1, x2) == (HEAP, 0, total)
            u.mem_write(x0, bytes(x2))
            trace.append(dict(operation='zero', bytes=x2))
        elif pc == base+0x1200:
            index = len(trace)-2
            assert (x0, x1, x2) == (HEAP+28+6144*index, ordered[index][0], 6144)
            u.mem_write(x0, bytes(u.mem_read(x1, x2)))
            trace.append(dict(operation='copy_record', offset=x0-HEAP, bytes=x2))
        else:
            raise AssertionError(hex(pc))
        u.reg_write(r.UC_ARM64_REG_PC, u.reg_read(r.UC_ARM64_REG_LR))

    for entry in (0xf03068, 0x14e0, 0x1200):
        u.hook_add(UC_HOOK_CODE, hook, begin=base+entry, end=base+entry)
    u.reg_write(r.UC_ARM64_REG_X0, CTX)
    u.reg_write(r.UC_ARM64_REG_SP, STACK+0x10000)
    u.reg_write(r.UC_ARM64_REG_LR, STOP)
    u.emu_start(base+0xc76488, STOP, count=100000)
    assert u.reg_read(r.UC_ARM64_REG_PC) == STOP
    assert u.reg_read(r.UC_ARM64_REG_SP) == STACK+0x10000
    status = u.reg_read(r.UC_ARM64_REG_X0)
    assert status == int(wrong_reservation and not disable)
    if disable or wrong_reservation:
        assert not trace and bytes(u.mem_read(CTX+0x34, 12)) == bytes(12)
        return None, status, trace
    assert len(trace) == count+2
    assert struct.unpack('<I', u.mem_read(CTX+0x34, 4))[0] == total
    assert struct.unpack('<Q', u.mem_read(CTX+0x38, 8))[0] == HEAP
    output = bytes(u.mem_read(HEAP, total))
    assert struct.unpack_from('<7I', output) == (0x300, 2, count, 6144, 28, 1024, offset)
    for i, (address, record) in enumerate(ordered):
        expected = bytearray(record)
        struct.pack_into('<I', expected, 0x1108, offset+1024*i)
        assert bytes(u.mem_read(address, 6144)) == expected
        assert output[28+6144*i:28+6144*(i+1)] == expected
    assert output[28+6144*count:offset] == bytes(offset-28-6144*count)
    assert output[offset:] == bytes(data_bytes)
    return output, status, trace


def main():
    path = ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == COMPILER_HASH
    pe = pefile.PE(str(path), fast_load=True)
    original = (ROOT/'results/windows-final-backend-output/kind-5.bin').read_bytes()
    assert hashlib.sha256(original).hexdigest() == STATISTICS_HASH
    records = [original[28+6144*i:28+6144*(i+1)] for i in range(4)]
    with tempfile.TemporaryDirectory(prefix='n1x-statistics-') as tmp:
        library = Path(tmp)/'statistics.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_statistics.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    parse = lib.n1x_dla_parse_statistics
    parse.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Layout)]
    parse.restype = ctypes.c_int

    def check(data, valid, *, length=None, null_input=False, null_output=False):
        # Deliberately unaligned input, with guards, exercises bytewise loads.
        storage = ctypes.create_string_buffer(b'\xa5'+data+b'\x5a')
        before = storage.raw
        out = Layout()
        ctypes.memset(ctypes.byref(out), 0xa5, ctypes.sizeof(out))
        code = parse(None if null_input else ctypes.addressof(storage)+1,
                     len(data) if length is None else length,
                     None if null_output else ctypes.byref(out))
        assert code == (0 if valid else -errno.EINVAL)
        assert storage.raw == before
        if not valid:
            assert bytes(out) == b'\xa5'*ctypes.sizeof(out)
            return None
        layout = {name: getattr(out, name) for name, _ in Layout._fields_}
        words = struct.unpack_from('<7I', data)
        assert layout == dict(count=words[2], metadata_offset=words[4], metadata_stride=words[3],
                              data_offset=words[6], data_stride=words[5])
        return layout

    cases = []
    for name, count, options, valid in (
        ('empty_enabled', 0, {}, True), ('single', 1, {}, True),
        ('second_enable_flag', 2, {'flag': 1}, True),
        ('exact_relu', 4, {}, True), ('thirteen_buckets', 26, {'distributed': True, 'flag': 2}, True),
        ('extra_active_graph_records', 4, {'extra_active': 2}, True),
        ('extra_data_capacity', 4, {'data_bytes': 4224}, True),
        ('producer_underallocated_data', 4, {'data_bytes': 3072}, False),
        ('disabled', 4, {'disable': True}, False),
        ('wrong_reservation', 4, {'wrong_reservation': True}, False),
    ):
        payload, status, trace = produce(pe, [records[i % 4] for i in range(count)], **options)
        layout = check(payload, valid) if payload is not None else None
        if name == 'exact_relu':
            assert payload == original
        cases.append(dict(case=name, producer_status=status, bytes=len(payload) if payload else 0,
                          parser_accepts=valid if payload is not None else None,
                          layout=layout, producer_trace=trace))

    rejects = []
    for length in (0, 1, 7, 27, 28, 24639, 24640, len(original)-1):
        check(original, False, length=length)
        rejects.append(f'truncated_to_{length}')
    for offset, value, name in (
        (0, 0x301, 'unknown_header'), (4, 1, 'old_revision'),
        (8, 0xffffffff, 'count_overflow'), (12, 0x340, 'old_record_stride'),
        (16, 0, 'metadata_overlaps_header'), (20, 0, 'zero_data_stride'),
        (24, 64, 'overlapping_regions'), (24, 24641, 'unaligned_data_region'),
        (24, 0xffffffc0, 'out_of_bounds_data_region'),
        (28+0x1108, 0, 'record_data_points_to_header'),
        (28+6144+0x1108, 24640, 'duplicate_record_data'),
        (28+3*6144+0x1108, len(original), 'last_record_data_outside_buffer'),
    ):
        candidate = bytearray(original)
        struct.pack_into('<I', candidate, offset, value)
        check(bytes(candidate), False)
        rejects.append(name)
    check(original, False, null_input=True)
    check(original, False, null_output=True)
    check(original, False, length=1 << 32)
    rejects.extend(['null_input', 'null_output', 'extent_exceeds_32_bit_format'])
    print(json.dumps(dict(passed=True, hardware_access=False, compiler_sha256=COMPILER_HASH,
        builder_rva='0xc76488', exact_relu_sha256=STATISTICS_HASH, cases=cases,
        rejected_cases=rejects, limitations=[
            'Builder uses synthetic graph/vector context and existing opaque metadata records',
            'Allocation, memset and memcpy are modeled; original header/count/offset logic executes',
            'Header words are preserved exactly; semantic major/minor byte ordering is not inferred',
            'The C parser validates immutable snapshots only; it is not connected to device DMA or a model loader',
            'Names, engine fields, execution-state entries, counters and timing semantics remain opaque',
            'No physical firmware boot, statistics collection or inference is established'
        ]), indent=2))


if __name__ == '__main__':
    main()
