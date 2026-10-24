#!/usr/bin/env python3
"""Execute Windows region builder, then feed its record to the firmware."""
import ctypes
import errno
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT, HOST, BLOB_HASH, run as run_firmware

BASE = 0x140000000
CTX, PAIR, BUFFER, STACK, STUB = 0x21000000, 0x21100000, 0x21200000, 0x21300000, 0x21400000
STOP = STUB+0xff0


def build(pe, region, dma=HOST, offset=256, size=512, provider_error=0, send_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    pages = (0xd49000, 0xd57000, 0xd39000, 0xd38000, 0xd3b000,
             0xcc000, 0xce000, 0xe3e000, 0x1512000, 0x13ac000)
    for page in pages:
        u.mem_map(BASE+page, 0x1000)
        if page == 0xd49000:
            u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address, length in ((CTX, 0x1000), (PAIR, 0x1000), (BUFFER, 0x1000),
                            (STACK, 0x10000), (STUB, 0x1000)):
        u.mem_map(address, length)
    ret = bytes.fromhex('c0035fd6')
    calls = (0xd57b60, 0xd57b80, 0xd39160, 0xd38a38, 0xd3b298,
             0xccee8, 0xce440, 0xce4b0, 0xd38758)
    for rva in calls:
        u.mem_write(BASE+rva, ret)
    for address in (STUB, STUB+0x10, STUB+0x20):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 3))
    u.mem_write(CTX+0x158, struct.pack('<Q', STUB+0x10))
    u.mem_write(CTX+0xf8, struct.pack('<Q', STUB+0x20))
    u.mem_write(PAIR, struct.pack('<QQ', BUFFER, dma))
    methods = []

    def reg(n):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))

    def hook(_, address, length, user):
        if address == STUB+0x10:
            assert reg(2) == region
            for n, value in ((3, 4096), (4, size), (5, offset)):
                u.mem_write(reg(n), struct.pack('<I', value))
            u.mem_write(reg(6), struct.pack('<Q', PAIR))
            u.reg_write(regs.UC_ARM64_REG_X0, provider_error)
        elif address == BASE+0xccee8:
            assert reg(1) == 4096 and reg(3) == 16
            u.mem_write(reg(0), bytes(u.mem_read(reg(2), reg(3))))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x20:
            methods.append({'command': reg(2), 'data': reg(3), 'argument_5': reg(5)})
            u.reg_write(regs.UC_ARM64_REG_X0, send_error)
        elif address in (BASE+0xd39160, BASE+0xd38a38):
            u.reg_write(regs.UC_ARM64_REG_X0, CTX)
        elif address in (BASE+0xd3b298, BASE+0xd38758, BASE+0xce440, BASE+0xce4b0):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X0, 0), (regs.UC_ARM64_REG_X1, CTX),
                            (regs.UC_ARM64_REG_X2, region), (regs.UC_ARM64_REG_SP, STACK+0x8000),
                            (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(register, value)
    u.emu_start(BASE+0xd49c20, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': u.reg_read(regs.UC_ARM64_REG_X0),
            'record': bytes(u.mem_read(BUFFER, 16)).hex(), 'methods': methods}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-dla-region-') as tmp:
        library = Path(tmp)/'region.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    serialize = lib.n1x_dla_build_region
    serialize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                           ctypes.c_uint64, ctypes.c_uint32]
    for region in (1, 3):
        for offset, size in ((256, 512), (512, 1024)):
            original = build(pe, region, offset=offset, size=size)
            output = ctypes.create_string_buffer(b'\xa5'*20, 20)
            assert serialize(output, 20, region, HOST+offset, size) == 0
            assert output.raw[:16].hex() == original['record']
            assert output.raw[16:] == b'\xa5'*4
    for count, region, address, size in ((15, 1, HOST, 512), (16, 4, HOST, 512),
                                        (16, 1, 0, 512), (16, 1, HOST+1, 512),
                                        (16, 1, HOST, 0), (16, 1, (1 << 64)-4, 8)):
        output = ctypes.create_string_buffer(b'\xa5'*20, 20)
        assert serialize(output, count, region, address, size) == -errno.EINVAL
        assert output.raw == b'\xa5'*20
    assert serialize(None, 16, 1, HOST, 512) == -errno.EINVAL
    rows = []
    for region in (1, 3, 5, 7):
        result = build(pe, region)
        assert result['status'] == 0
        assert result['record'] == struct.pack('<IQI', region, HOST+256, 512).hex()
        assert result['methods'] == [{'command': 11, 'data': HOST >> 8, 'argument_5': 0}]
        payload = bytearray(1024)
        payload[:16] = bytes.fromhex(result['record'])
        reply = run_firmware(blob, method=0x30b, payload=payload)
        assert reply['returned']
        assert reply['interrupts'] == [(0xb02 if region in (1, 3) else 0xb0b01, HOST >> 8)]
        rows.append({'case': f'windows_region_{region}_firmware_roundtrip', **result, 'firmware': reply})
    result = build(pe, 1, provider_error=0x65)
    assert not result['methods'] and result['record'] == bytes(16).hex()
    rows.append({'case': 'provider_failure_no_record_or_command', **result})
    result = build(pe, 1, send_error=0x65)
    assert result['status'] == 0x65 and len(result['methods']) == 1
    rows.append({'case': 'send_failure_propagated', **result})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_sha256': KMD_SHA256, 'firmware_sha256': BLOB_HASH,
                      'windows_builder_rva': '0xd49c20', 'cases': rows,
                      'linux_serializer_differential_cases': 4, 'linux_preflight_failure_cases': 7,
                      'limitations': ['Provider uses synthetic CPU/DMA addresses and buffer sizes',
                                      'Cache maintenance, locking, logging and OS helpers are stubbed',
                                      'Windows configuration flags selecting regions 5/7 are not established',
                                      'No physical firmware initialization or inference occurred']}, indent=2))


if __name__ == '__main__':
    main()
