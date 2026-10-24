#!/usr/bin/env python3
"""Differential SROOT TX: original ARM64 routine versus callback-only Linux C."""
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
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ
from unicorn import arm64_const as regs
from test_core_control import IO, WRITE, READ, DELAY

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
HASH = 'f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a'
CTX, DESC, PACKET, STACK, MMIO, STOP = (0x21000000+i*0x10000 for i in range(6))


def windows(pe, stride, command, words, corrupt=None):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x4000, 0x2000), (0x1b000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, DESC, PACKET, STACK, MMIO, STOP):
        u.mem_map(address, 0x10000)
    u.mem_write(BASE+0x1b358, struct.pack('<Q', BASE+0x1b358))
    u.mem_write(CTX+0x38, struct.pack('<Q', DESC))
    u.mem_write(DESC+0x50, struct.pack('<I', stride))
    u.mem_write(DESC+0x58, struct.pack('<Q', MMIO))
    u.mem_write(PACKET+4, bytes([command]))
    u.mem_write(PACKET+8, struct.pack('<I', len(words)))
    for i, word in enumerate(words):
        u.mem_write(PACKET+12+4*i, struct.pack('<I', word))
    tx, trace, position, echo_ack = [], [], 0, False

    def memory(_, access, address, size, value, user):
        nonlocal position, echo_ack
        offset = address-MMIO
        assert size == 4
        if access == UC_MEM_READ:
            if offset in (2*stride, 8*stride):
                value = int(offset != 8*stride or not echo_ack)
            elif offset == 7*stride:
                value = tx[position] ^ 0xffffffff
                if position == corrupt:
                    value ^= 1
                position += 1
            elif offset == 5*stride:
                value = 0  # Windows error cleanup: no pending RX packet.
            else:
                raise AssertionError(('unexpected read', offset))
            u.mem_write(address, struct.pack('<I', value))
            trace.append(('r', offset, value))
        else:
            assert offset in (stride, 3*stride, 9*stride), offset
            trace.append(('w', offset, value))
            if offset == stride:
                tx.append(value)
            elif offset == 9*stride:
                echo_ack = True

    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory, begin=MMIO, end=MMIO+0xffff)
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_X1, PACKET)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x5130, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return u.reg_read(regs.UC_ARM64_REG_X0), trace


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    send = lib.n1x_sroot_send
    send.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint8,
                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32]

    def linux(stride, command, words, corrupt=None, fail_at=None, not_ready=None, polls=1):
        trace, tx, position = [], [], 0

        @WRITE
        def write(_, offset, value):
            trace.append(('w', offset, value))
            if offset == stride:
                tx.append(value)
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @READ
        def read(_, offset, output):
            nonlocal position
            if offset in (2*stride, 8*stride):
                value = int(offset != not_ready)
            elif offset == 7*stride:
                value = tx[position] ^ 0xffffffff
                if position == corrupt:
                    value ^= 1
                position += 1
            else:
                raise AssertionError(offset)
            output[0] = value
            trace.append(('r', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            trace.append(('d', interval))

        io = IO(None, write, read, delay)
        data = (ctypes.c_uint32*len(words))(*words)
        status = send(ctypes.byref(io), stride, command, data, len(words), polls, 1)
        return status, trace

    cases = 0
    for stride in (4, 8, 16):
        for count in (0, 1, 3, 14):
            words = [(i*0x1234567+0x80000000) & 0xffffffff for i in range(count)]
            expected = windows(pe, stride, 0x18, words)
            assert expected[0] == 0
            assert linux(stride, 0x18, words) == expected
            cases += 1
    words = [0, 0x80000000, 0xffffffff]
    for corrupt in range(4):
        status, trace = windows(pe, 4, 0x18, words, corrupt)
        assert status == 3
        # Windows cleanup drains pending RX and echo status; Linux requires recovery.
        cleanup = trace.index(('r', 20, 0))
        assert linux(4, 0x18, words, corrupt) == (-errno.EIO, trace[:cleanup])
    expected = linux(4, 0x18, words)[1]
    for i in range(len(expected)):
        assert linux(4, 0x18, words, fail_at=i) == (-errno.EIO, expected[:i+1])
    assert linux(4, 0x18, words, not_ready=8, polls=3) == (
        -errno.ETIMEDOUT, [('r', 8, 0), ('d', 1), ('r', 8, 0), ('d', 1), ('r', 8, 0)])
    status, trace = linux(4, 0x18, words, not_ready=32, polls=3)
    assert status == -errno.ETIMEDOUT and trace[-5:] == [
        ('r', 32, 0), ('d', 1), ('r', 32, 0), ('d', 1), ('r', 32, 0)]
    assert not any(e[:2] == ('w', 36) for e in trace)
    for stride in (0, 1, 0xfffffffc):
        assert linux(stride, 0, []) == (-errno.EINVAL, [])
    for polls in (0, 5002):
        assert linux(4, 0, [], polls=polls) == (-errno.EINVAL, [])
    assert linux(4, 0, [0]*15) == (-errno.EINVAL, [])
    print(json.dumps({'passed': True, 'hardware_access': False, 'driver_sha256': HASH,
                      'original_rva': '0x5130', 'matching_success_traces': cases,
                      'echo_corruption_cases': 4, 'callback_failure_positions': len(expected),
                      'limitations': ['Synthetic stride and register readiness',
                                      'TX echo only; no firmware reply, DMA allocation or device boot',
                                      'Linux bounds polling and leaves error recovery to the caller']}, indent=2))


if __name__ == '__main__':
    main()
