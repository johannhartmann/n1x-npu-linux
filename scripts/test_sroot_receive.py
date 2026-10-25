#!/usr/bin/env python3
"""Original SROOT RX/decoder execution compared with Linux callback component."""
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
from test_sroot_send import ROOT, BASE, HASH, CTX, DESC, PACKET, STACK, MMIO, STOP


class Reply(ctypes.Structure):
    _fields_ = [('header', ctypes.c_uint32), ('words', ctypes.c_uint32*14),
                ('status', ctypes.c_uint16), ('command', ctypes.c_uint8),
                ('detail', ctypes.c_uint8), ('count', ctypes.c_uint8)]


def windows(pe, stride, header, words):
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
    trace, position = [], 0
    data = [header]+words

    def memory(_, access, address, size, value, user):
        nonlocal position
        offset = address-MMIO
        assert size == 4
        if access == UC_MEM_READ:
            if offset == 5*stride:
                value = 1
            else:
                assert offset == 4*stride
                value = data[position]
                position += 1
            u.mem_write(address, struct.pack('<I', value))
            trace.append(('r', offset, value))
        else:
            assert offset == 6*stride and value == 1
            trace.append(('w', offset, value))

    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory, begin=MMIO, end=MMIO+0xffff)
    for index, value in enumerate((CTX, PACKET, 0, DESC)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x5328, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert u.reg_read(regs.UC_ARM64_REG_X0) == 0
    packet = bytes(u.mem_read(PACKET, 12+4*len(words)))
    decoded, count = struct.unpack_from('<I', packet)[0], struct.unpack_from('<I', packet, 8)[0] & 15
    return trace, (packet[4], decoded & 4095, (decoded >> 12) & 255, count,
                   list(struct.unpack_from('<'+'I'*count, packet, 12)))


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-rx-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    receive = lib.n1x_sroot_receive
    receive.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.POINTER(Reply),
                        ctypes.c_uint32, ctypes.c_uint32]

    def linux(stride, header, words, fail_at=None, ready=True, polls=1):
        trace, position = [], 0
        data = [header]+words
        reply = Reply()
        ctypes.memset(ctypes.byref(reply), 0xa5, ctypes.sizeof(reply))
        before = bytes(reply)

        @READ
        def read(_, offset, output):
            nonlocal position
            if offset == 5*stride:
                value = int(ready)
            else:
                assert offset == 4*stride
                value = data[position]
                position += 1
            output[0] = value
            trace.append(('r', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @WRITE
        def write(_, offset, value):
            assert offset == 6*stride and value == 1
            trace.append(('w', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            trace.append(('d', interval))

        io = IO(None, write, read, delay)
        status = receive(ctypes.byref(io), stride, ctypes.byref(reply), polls, 1)
        if status:
            assert bytes(reply) == before  # No partially valid reply escapes.
            return status, trace, None
        assert reply.header == header
        return status, trace, (reply.command, reply.status, reply.detail, reply.count,
                               list(reply.words[:reply.count]))

    cases = 0
    for stride in (4, 8, 16):
        for count in (0, 1, 3, 14):
            for status, detail in ((0, 0), (1, 0x80), (0xfff, 0xff)):
                header = 0x18 | count << 8 | status << 12 | detail << 24
                words = [(0x87654321*i) & 0xffffffff for i in range(count)]
                trace, decoded = windows(pe, stride, header, words)
                assert linux(stride, header, words) == (0, trace, decoded)
                cases += 1
    # Actual first P16 SROOT reply; original decoder confirms fields only,
    # not the undocumented meanings of status 6 / detail 0x2a.
    live_trace, live_decoded = windows(pe, 16, 0x2a006018, [])
    assert live_decoded == (0x18, 6, 0x2a, 0, [])
    assert linux(16, 0x2a006018, []) == (0, live_trace, live_decoded)
    header, words = 0x12300318, [0, 0xffffffff, 0x80000000]
    trace, _ = windows(pe, 4, header, words)
    for i in range(len(trace)):
        assert linux(4, header, words, fail_at=i) == (-errno.EIO, trace[:i+1], None)
    assert linux(4, 0xf18, []) == (-errno.EMSGSIZE, [('r', 20, 1), ('r', 16, 0xf18)], None)
    assert linux(4, header, words, ready=False, polls=3) == (
        -errno.ETIMEDOUT, [('r', 20, 0), ('d', 1), ('r', 20, 0), ('d', 1), ('r', 20, 0)], None)
    for stride in (0, 1, 0xfffffffc):
        assert linux(stride, 0, []) == (-errno.EINVAL, [], None)
    for polls in (0, 5002):
        assert linux(4, 0, [], polls=polls) == (-errno.EINVAL, [], None)
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_rva': '0x5328',
                      'decoder_rva': '0x4f70', 'matching_reply_traces': cases,
                      'callback_failure_positions': len(trace),
                      'first_live_reply': {'header': '0x2a006018', 'decoded': live_decoded},
                      'limitations': ['Synthetic reply data and register spacing',
                                      'Status meaning and request correlation remain caller responsibilities',
                                      'No DMA ownership, bus preparation or hardware firmware boot']}, indent=2))


if __name__ == '__main__':
    main()
