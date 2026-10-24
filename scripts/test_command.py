#!/usr/bin/env python3
"""Compare the Linux method sender with the original Windows ARM64 function."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import struct
import subprocess
import tempfile
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from test_core_control import IO, WRITE, READ, DELAY

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
OBJ, TABLE, STACK, STUB = 0x20000000, 0x20100000, 0x20200000, 0x20300000
STOP = STUB+0xff0
METHOD, DATA = 0x301, 0x123456
SLOTS = {0x190: ('read', 0x68), 0x198: ('write', 0x68), 0x1a8: ('write', 0x6c)}


def windows(pe, busy=0, fail_at=None):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0xd4c000, 0x1000), (BASE+0xd57000, 0x1000),
                       (BASE+0xd8000, 0x1000), (BASE+0xce000, 0x1000),
                       (BASE+0x13ac000, 0x1000), (BASE+0xe3e000, 0x1000),
                       (OBJ, 0x1000), (TABLE, 0x1000), (STACK, 0x10000), (STUB, 0x1000)):
        uc.mem_map(addr, size)
    uc.mem_write(BASE+0xd4c800, pe.get_data(0xd4c800, 0x16c))
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd57b60, 0xd57b80, 0xd82c0, 0xce4b0):
        uc.mem_write(BASE+rva, ret)
    uc.mem_write(STUB+0x800, ret)
    uc.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB+0x800))
    uc.mem_write(OBJ, struct.pack('<Q', TABLE))
    for slot in SLOTS:
        uc.mem_write(TABLE+slot, struct.pack('<Q', STUB+slot))
        uc.mem_write(STUB+slot, ret)
    trace, reads, delays = [], [], []

    def hook(_, address, size, user):
        slot = address-STUB
        if slot in SLOTS:
            operation, offset = SLOTS[slot]
            pointer = uc.reg_read(regs.UC_ARM64_REG_X2)
            if operation == 'read':
                value = (1 << 16) if len(reads) < busy else 0
                reads.append(value)
                uc.mem_write(pointer, struct.pack('<I', value))
            else:
                value = struct.unpack('<I', uc.mem_read(pointer, 4))[0]
            trace.append((operation, offset, value))
            uc.reg_write(regs.UC_ARM64_REG_X0, 0x65 if len(trace)-1 == fail_at else 0)
        elif address == BASE+0xd82c0:
            delays.append(uc.reg_read(regs.UC_ARM64_REG_X0))

    uc.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0, OBJ, DATA, METHOD)):
        uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
    uc.emu_start(BASE+0xd4c800, STOP, count=1000000)
    assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return uc.reg_read(regs.UC_ARM64_REG_X0), trace, reads, delays


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-dla-command-') as tmp:
        library = Path(tmp)/'command.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    send = lib.n1x_dla_send_method
    send.argtypes = [ctypes.POINTER(IO)]+[ctypes.c_uint32]*4

    def linux(busy=0, fail_at=None, polls=5001, interval=100, no_delay=False):
        trace, reads, delays = [], [], []

        @WRITE
        def write(_, offset, value):
            trace.append(('write', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @READ
        def read(_, offset, pointer):
            value = 1 << 16 if len(reads) < busy else 0
            reads.append(value)
            trace.append(('read', offset, value))
            pointer[0] = value
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, value):
            delays.append(value)

        io = IO(None, write, read, DELAY() if no_delay else delay)
        status = send(ctypes.byref(io), METHOD, DATA, polls, interval)
        return status, trace, reads, delays

    for busy in (0, 3, 4999):
        assert linux(busy) == windows(pe, busy)
    for fail_at in (0, 1, 2):
        code, trace, reads, delays = linux(fail_at=fail_at)
        reference, win_trace, win_reads, win_delays = windows(pe, fail_at=fail_at)
        assert code == -errno.EIO and reference == 0x65
        assert (trace, reads, delays) == (win_trace, win_reads, win_delays)
    code, trace, reads, delays = linux(busy=9999)
    assert code == -errno.ETIMEDOUT and len(reads) == 5001 and len(delays) == 5000
    assert all(row[0] == 'read' for row in trace)
    reference, win_trace, win_reads, win_delays = windows(pe, busy=9999)
    assert reference == 0x65 and len(win_reads) == 5002 and len(win_delays) == 5001
    for kwargs in ({'polls': 0}, {'polls': 5002}, {'interval': 0}, {'no_delay': True}):
        code, trace, _, _ = linux(**kwargs)
        assert code == -errno.EINVAL and not trace
    assert linux(polls=1, interval=0, no_delay=True)[0] == 0
    assert send(None, METHOD, DATA, 1, 0) == -errno.EINVAL
    print(json.dumps({'scope': 'Linux C sender versus original Windows ARM64 RVA 0xd4c800 in RAM',
                      'successful_traces_match': True, 'callback_failures_match_after_errno_translation': True,
                      'linux_busy_timeout_reads': 5001, 'windows_busy_timeout_reads': 5002,
                      'write_order': ['MTHDWDAT 0x6c', 'MTHDID 0x68'],
                      'pending_bit': 16, 'completion_not_tested': True,
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
