#!/usr/bin/env python3
"""Compare C mailbox field decoding with the original Windows HAL-1 parser."""
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
from arm64_stack_cookie import install
from extract_firmware import KMD_SHA256
from test_response import Response, ResponseFields as Fields

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, VTABLE, OUT, STACK, STUB = (0x29000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


def windows(pe, word, data=0x123456, fail_at=None):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x1951000, 0xd000), (0xd48000, 0x3000), (0xd3c000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address, size in ((BASE+0xd57000, 0x1000), (BASE+0xd39000, 0x1000),
                          (BASE+0xe3e000, 0x1000), (BASE+0xce000, 0x1000),
                          (CTX, 0x10000), (VTABLE, 0x10000), (OUT, 0x10000),
                          (STACK, 0x10000), (STUB, 0x10000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for address in (STUB, BASE+0xd39878, BASE+0xce4b0):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(CTX+0x496, struct.pack('<H', 1))
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x1951238, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    u.mem_write(CTX+0x68, struct.pack('<Q', CTX+8))
    u.mem_write(CTX+8, struct.pack('<Q', VTABLE))
    u.mem_write(VTABLE+0xd0, pe.get_data(0x108e9b8+0xd0, 8))
    u.mem_write(OUT, struct.pack('<III', *([0xa5a5a5a5]*3)))
    trace = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39878:
            assert reg(1) == 0 and reg(2) in (0x40, 0x44)
            trace.append(reg(2))
            u.mem_write(reg(3), struct.pack('<I', word if reg(2) == 0x40 else data))
            u.reg_write(regs.UC_ARM64_REG_X0, 0x65 if len(trace)-1 == fail_at else 0)
        elif address == BASE+0xce4b0:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0x12340000, CTX, OUT, OUT+4, OUT+8)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd48910, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    command, message, status = struct.unpack('<III', u.mem_read(OUT, 12))
    return reg(0), (message, command, status), trace


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-response-decode-') as tmp:
        library = Path(tmp)/'response.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    decode = lib.n1x_dla_decode_response
    decode.argtypes = [ctypes.POINTER(Response), ctypes.POINTER(Fields)]
    words = {0, 0xffffffff, 0x102, 0x40101, 0x110101, 0x702, 0x90701, 0x8003070a}
    for shift in (0, 8, 16, 24):
        words.update(value << shift for value in range(256))
    for word in sorted(words):
        response, output = Response(word, 0x123456), Fields()
        assert decode(ctypes.byref(response), ctypes.byref(output)) == 0
        code, actual, reads = windows(pe, word)
        assert code == 0 and reads == [0x40, 0x44]
        assert actual == (output.message, output.command, output.status)
        assert output.upper == word >> 24 and output.data == response.data
    for fail_at in (0, 1):
        status, fields, reads = windows(pe, 0x8003070a, fail_at=fail_at)
        assert status == 0x65 and fields == (0xa5a5a5a5,)*3
        assert reads == [0x40, 0x44][:fail_at+1]
    output = Fields(1, 2, 3, 4, 5)
    assert decode(None, ctypes.byref(output)) == -errno.EINVAL
    assert (output.message, output.command, output.status, output.upper, output.data) == (1, 2, 3, 4, 5)
    assert decode(ctypes.byref(Response()), None) == -errno.EINVAL
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_parser': '0xd48910',
                      'differential_words': len(words), 'reader_error_cases': 2,
                      'limitations': ['Mailbox reads are synthetic; HAL initializer, parser and accessors execute',
                                      'Upper byte and data are preserved without assigning semantics',
                                      'Decoding alone does not establish task completion or DMA quiescence']}, indent=2))


if __name__ == '__main__':
    main()
