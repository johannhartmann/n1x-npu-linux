#!/usr/bin/env python3
"""Compare C recent-history snapshots with original Windows region-5 readers."""
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
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from emulate_region5_setup import ROOT, BASE, HOST, KMD_SHA256, ALLOC_BYTES, RING_BYTES

CTX, DEVICE, OUTER, OUTPUT, STACK, STUB = (0x37000000+i*0x10000 for i in range(6))
BUFFER = 0x38000000
STOP = STUB+0xff0
STRIDE, LIMIT = 52, 50
CAPACITY = (RING_BYTES-8)//STRIDE


def windows(pe, ring, *, latest=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd46000, 0xd47000, 0xd57000, 0xd39000, 0xd3b000,
                 0xcd000, 0xcc000, 0xe3e000, 0x1512000, 0x61a000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, DEVICE, OUTER, OUTPUT, STACK, STUB):
        u.mem_map(address, 0x10000)
    u.mem_map(BUFFER, ALLOC_BYTES)
    u.mem_write(BUFFER+16, bytes(ring))
    u.mem_write(OUTPUT, b'\xa5'*(4+LIMIT*STRIDE+16))
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6'))
    for address, value in ((BASE+0xe3eaf0, STUB), (BASE+0x1512148, DEVICE),
                           (CTX+0x158, BASE+0xd470d0), (CTX+0x440, BASE+0x61a7f0),
                           (CTX+0x4c8, BUFFER), (CTX+0x4d0, HOST)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(CTX+0x480, struct.pack('<I', 1))
    copies, cache = [], []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            ret(OUTER)
        elif address == BASE+0xd3b298:
            assert [reg(i) for i in range(4)] == [OUTER, BUFFER, ALLOC_BYTES, 0]
            cache.append(0)
            ret(0)
        elif address == BASE+0xccee8:
            assert reg(3) <= reg(1)
            assert OUTPUT+4 <= reg(0) and reg(0)+reg(3) <= OUTPUT+4+LIMIT*STRIDE
            assert BUFFER+16+8 <= reg(2) and reg(2)+reg(3) <= BUFFER+16+RING_BYTES
            copies.append([reg(2)-(BUFFER+16), reg(3)])
            u.mem_write(reg(0), bytes(u.mem_read(reg(2), reg(3))))
            ret(0)

    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((DEVICE, CTX, OUTPUT, OUTPUT+4)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+(0xd46450 if latest else 0xd465a0), STOP, count=20000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP and u.reg_read(regs.UC_ARM64_REG_X0) == 0
    assert cache == [0]
    assert bytes(u.mem_read(BUFFER+16, RING_BYTES)) == ring  # Neither reader consumes.
    return bytes(u.mem_read(OUTPUT, 4+LIMIT*STRIDE+16)), copies


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-history-') as tmp:
        library = Path(tmp)/'ring.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_ring.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    snapshot = lib.n1x_dla_region5_history
    snapshot.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
    records = b''.join(struct.pack('<I', i)+bytes(((i+j)&255) for j in range(4, STRIDE))
                       for i in range(CAPACITY))
    rows = []
    for first, second in ((0, 0), (0, 1), (0, 49), (0, 50), (0, 51), (0, CAPACITY),
                           (1, 1), (1, 2), (100, 300), (CAPACITY-1, 1),
                           (CAPACITY, 0), (CAPACITY, CAPACITY), (CAPACITY-50, CAPACITY-1)):
        ring = struct.pack('<II', 8+first*STRIDE, 8+second*STRIDE)+records
        original, copies = windows(pe, ring)
        output = ctypes.create_string_buffer(b'\xa5'*(LIMIT*STRIDE+16), LIMIT*STRIDE+16)
        count = ctypes.c_uint32(0xa5a5a5a5)
        assert snapshot(ring, len(ring), output, LIMIT*STRIDE, ctypes.byref(count)) == 0
        if first == second:
            assert count.value == 0 and output.raw == b'\xa5'*len(output)
            assert original == b'\xa5'*len(original) and not copies
        else:
            expected_count = min(second, LIMIT) if first == 0 else LIMIT
            assert count.value == expected_count
            assert original == struct.pack('<I', count.value)+output.raw
            expected_indices = [(second-count.value+i)%CAPACITY for i in range(count.value)]
            assert output.raw[:count.value*STRIDE] == b''.join(records[i*STRIDE:(i+1)*STRIDE] for i in expected_indices)
        rows.append({'first_index': first, 'second_index': second, 'count': count.value,
                     'windows_copy_spans': copies})
    initial = bytes(8)+records
    initial_windows, copies = windows(pe, initial)
    assert initial_windows == b'\xa5'*len(initial_windows) and not copies
    initial_out = ctypes.create_string_buffer(b'\xa5'*52, 52)
    initial_count = ctypes.c_uint32(123)
    assert snapshot(initial, len(initial), initial_out, len(initial_out), ctypes.byref(initial_count)) == 0
    assert initial_count.value == 0 and initial_out.raw == b'\xa5'*52
    # The separate Windows last-record query recognizes tag 1 and returns raw
    # byte 2 followed by byte 1. Do not guess their semantic meanings.
    for tag in (1, 2):
        ring = bytearray(struct.pack('<II', 8, 8+STRIDE)+records)
        ring[8:11] = bytes((tag, 0x7a, 0xb5))
        original, copies = windows(pe, bytes(ring), latest=True)
        assert struct.unpack('<II', original[:8]) == ((0xb5, 0x7a) if tag == 1 else (0, 0))
        assert not copies
    ring = struct.pack('<II', 8, 8+STRIDE)+records
    failures = 0
    for header in ((0, 60), (7, 60), (9, 60), (8, 61), (8, RING_BYTES+52),
                   (RING_BYTES+52, 60)):
        invalid = struct.pack('<II', *header)+records
        out = ctypes.create_string_buffer(b'\xa5'*(LIMIT*STRIDE), LIMIT*STRIDE)
        count = ctypes.c_uint32(0xa5a5a5a5)
        assert snapshot(invalid, len(invalid), out, len(out), ctypes.byref(count)) == -errno.EINVAL
        assert count.value == 0xa5a5a5a5 and out.raw == b'\xa5'*len(out)
        failures += 1
    for size in (0, 7, 8+49*STRIDE, RING_BYTES-1, 1 << 32):
        count = ctypes.c_uint32(0xa5a5a5a5)
        assert snapshot(ring, size, None, 0, ctypes.byref(count)) == -errno.EINVAL
        assert count.value == 0xa5a5a5a5
        failures += 1
    for capacity in (0, 51):
        out = ctypes.create_string_buffer(b'\xa5'*52, 52)
        count = ctypes.c_uint32(0xa5a5a5a5)
        assert snapshot(ring, len(ring), out, capacity, ctypes.byref(count)) == -errno.ENOSPC
        assert count.value == 0xa5a5a5a5 and out.raw == b'\xa5'*52
        failures += 1
    minimum = struct.pack('<II', 8+STRIDE, 8+2*STRIDE)+records[:LIMIT*STRIDE]
    out = ctypes.create_string_buffer(b'\xa5'*(LIMIT*STRIDE+16), LIMIT*STRIDE+16)
    count = ctypes.c_uint32(123)
    assert snapshot(minimum, len(minimum), out, LIMIT*STRIDE, ctypes.byref(count)) == 0
    assert count.value == LIMIT
    assert out.raw == records[2*STRIDE:LIMIT*STRIDE]+records[:2*STRIDE]+b'\xa5'*16
    count = ctypes.c_uint32(123)
    assert snapshot(None, len(ring), None, 0, ctypes.byref(count)) == -errno.EINVAL
    assert snapshot(ring, len(ring), None, 0, None) == -errno.EINVAL
    assert snapshot(ring, len(ring), None, 52, ctypes.byref(count)) == -errno.ENOSPC
    empty = struct.pack('<II', 8, 8)+records
    assert snapshot(empty, len(empty), None, 0, ctypes.byref(count)) == 0 and count.value == 0
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_history_reader': '0xd465a0', 'windows_last_record_query': '0xd46450',
                      'cases': rows, 'invalid_extent_header_capacity_cases': failures,
                      'minimum_capacity_wrap_control': True, 'initial_zero_header_empty': True,
                      'limitations': ['OS/cache/copy boundaries are synthetic; original provider/readers execute',
                                      'This is recent-history copying, not consuming or task completion',
                                      'Record fields remain opaque except the observed tag/byte query',
                                      'Only immutable CPU snapshots are accepted as a caller contract',
                                      'No live DMA ring atomicity, firmware production or numerical inference is established']}, indent=2))


if __name__ == '__main__':
    main()
