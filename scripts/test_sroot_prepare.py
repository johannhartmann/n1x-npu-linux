#!/usr/bin/env python3
"""Verify mailbox preparation against original ARM64, without hardware access."""
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
from test_sroot_send import ROOT, BASE, HASH, CTX, DESC, STACK, MMIO, STOP


def windows(pe, stride, rx_pending, echo_pending, busy):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x4000, 0x2000), (0x1b000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, DESC, STACK, MMIO, STOP):
        u.mem_map(address, 0x10000)
    u.mem_write(BASE+0x1b358, struct.pack('<Q', BASE+0x1b358))
    u.mem_write(CTX+0x38, struct.pack('<Q', DESC))
    u.mem_write(DESC+0x50, struct.pack('<I', stride))
    u.mem_write(DESC+0x58, struct.pack('<Q', MMIO))
    trace = []
    pending = {5: rx_pending, 8: echo_pending}

    def memory(_, access, address, size, value, user):
        index = (address-MMIO)//stride
        assert size == 4 and (address-MMIO) % stride == 0
        if access == UC_MEM_READ:
            if index in pending:
                value = int(pending[index] > 0)
            elif index == 2:
                value = 1
            else:
                assert index == 11
                value = busy
            u.mem_write(address, struct.pack('<I', value))
            trace.append(('r', index*stride, value))
        else:
            assert (index, value) in ((12, 0xffffffff), (3, 3), (6, 1), (9, 1))
            if index in (6, 9):
                pending[index-1] -= 1
            trace.append(('w', index*stride, value))

    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory, begin=MMIO, end=MMIO+0xffff)
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x54c0, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return u.reg_read(regs.UC_ARM64_REG_X0), trace


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-prepare-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    prepare = lib.n1x_sroot_prepare
    prepare.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]

    def linux(stride, rx_pending=0, echo_pending=0, busy=0, fail_at=None, polls=4, ready=True):
        trace, pending = [], {5: rx_pending, 8: echo_pending}

        @READ
        def read(_, offset, output):
            index = offset//stride
            if index in pending:
                value = int(pending[index] > 0)
            elif index == 2:
                value = int(ready)
            else:
                assert index == 11
                value = busy
            output[0] = value
            trace.append(('r', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @WRITE
        def write(_, offset, value):
            index = offset//stride
            trace.append(('w', offset, value))
            if index in (6, 9):
                pending[index-1] -= 1
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            trace.append(('d', interval))

        io = IO(None, write, read, delay)
        return prepare(ctypes.byref(io), stride, polls, 1), trace

    cases = 0
    for stride in (4, 8, 16):
        for rx, echo in ((0, 0), (1, 0), (0, 2), (2, 2)):
            for busy in (0, 1, 0x100, 0xffffffff):
                original, expected = windows(pe, stride, rx, echo, busy)
                status, trace = linux(stride, rx, echo, busy)
                assert original == busy & 255
                assert status == (-errno.EBUSY if original else 0)
                assert [event for event in trace if event[0] != 'd'] == expected
                assert trace.count(('d', 1)) == rx+echo
                cases += 1
    _, trace = linux(16, 2, 2)
    failures = [i for i, event in enumerate(trace) if event[0] != 'd']
    for i in failures:
        assert linux(16, 2, 2, fail_at=i) == (-errno.EIO, trace[:i+1])
    for rx, echo, status_offset in ((20, 0, 80), (0, 20, 128)):
        status, trace = linux(16, rx, echo, polls=3)
        assert status == -errno.ETIMEDOUT and trace[-1] == ('r', status_offset, 1)
        assert sum(event[:2] == ('r', status_offset) for event in trace) == 3
        assert not any(event[:2] == ('r', 32) for event in trace)
    status, trace = linux(16, ready=False, polls=3)
    assert status == -errno.ETIMEDOUT and trace[-5:] == [
        ('r', 32, 0), ('d', 1), ('r', 32, 0), ('d', 1), ('r', 32, 0)]
    for stride in (0, 1, 0xfffffffc):
        assert linux(stride) == (-errno.EINVAL, [])
    for polls in (0, 5002):
        assert linux(16, polls=polls) == (-errno.EINVAL, [])
    assert linux(16, polls=1)[0] == 0
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_rva': '0x54c0',
                      'matching_mmio_traces': cases, 'callback_failure_positions': len(failures),
                      'limitations': ['Synthetic status transitions and no physical device mapping',
                                      'Pending notifications are discarded; caller must own idle mailbox',
                                      'No proof of DMA quiescence, firmware upload or device reset semantics']}, indent=2))


if __name__ == '__main__':
    main()
