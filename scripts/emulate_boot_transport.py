#!/usr/bin/env python3
"""Trace original boot wrapper/transport up to IoBuildDeviceIoControlRequest.

The emulator stops at the OS call, before an IRP is built or sent. No hardware.
"""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
CTX, HANDLE, DATA, STACK, STUB = 0x21000000, 0x21100000, 0x21200000, 0x21300000, 0x21400000


def run(pe, subtype=0, available=True, length=16):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page, size in ((0xd49000, 0x2000), (0xd3b000, 0x1000), (0xd3c000, 0x1000),
                       (0xd57000, 0x1000), (0xd39000, 0x1000),
                       (0x1512000, 0x1000), (0xe3e000, 0x1000)):
        u.mem_map(BASE+page, size)
        if page in (0xd49000, 0xd3b000, 0xd3c000):
            u.mem_write(BASE+page, pe.get_data(page, size))
    for address, size in ((CTX, 0x1000), (HANDLE, 0x1000), (DATA, 0x1000), (STACK, 0x10000), (STUB, 0x1000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160,):
        u.mem_write(BASE+rva, ret)
    for address in (STUB, STUB+0x10):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3e180, struct.pack('<Q', STUB))
    u.mem_write(BASE+0xe3e450, struct.pack('<Q', STUB+0x10))
    u.mem_write(CTX+0x308, struct.pack('<Q', HANDLE if available else 0))
    u.mem_write(CTX+0x54, struct.pack('<I', 1))
    u.mem_write(HANDLE+0x18, struct.pack('<Q', 0x21500000))
    u.mem_write(DATA, bytes(range(16)))
    calls = []

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, CTX)
        elif address == STUB:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x10:
            calls.append({'ioctl': hex(reg(0)), 'device_object': hex(reg(1)),
                          'input': bytes(u.mem_read(reg(2), reg(3))).hex(), 'input_bytes': reg(3),
                          'output_pointer': reg(4), 'output_bytes': reg(5), 'internal': reg(6)})
            u.emu_stop()

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in enumerate((CTX, CTX, DATA, length, subtype)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STUB+0xff0)
    u.emu_start(BASE+0xd49ef0, STUB+0xff0, count=10000)
    returned = u.reg_read(regs.UC_ARM64_REG_PC) == STUB+0xff0
    return {'returned': returned, 'status': reg(0) if returned else None, 'os_boundary': calls}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path))
    imports = {item.address: item.name for entry in pe.DIRECTORY_ENTRY_IMPORT for item in entry.imports}
    assert imports[BASE+0xe3e180] == b'KeInitializeEvent'
    assert imports[BASE+0xe3e450] == b'IoBuildDeviceIoControlRequest'
    assert imports[BASE+0xe3e458] == b'IofCallDriver'
    rows = []
    for subtype in (0, 1):
        result = run(pe, subtype)
        expected = {'ioctl': '0x150214' if subtype == 0 else '0x150218',
                    'device_object': '0x21500000',
                    'input': bytes(range(16)).hex() if subtype == 0 else '02030c0002000000',
                    'input_bytes': 16 if subtype == 0 else 8,
                    'output_pointer': 0, 'output_bytes': 0, 'internal': 0}
        assert not result['returned'] and result['os_boundary'] == [expected]
        rows.append({'subtype': subtype, **result})
    result = run(pe, available=False)
    assert result['returned'] and result['status'] == 0xffff and not result['os_boundary']
    rows.append({'case': 'missing_auxiliary_handle', **result})
    result = run(pe, length=0)
    assert result['returned'] and result['status'] == 0x1f and not result['os_boundary']
    rows.append({'case': 'empty_image_rejected', **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'wrapper': '0xd49ef0', 'transport': '0xd3bbc8', 'cases': rows,
                      'limitations': ['Stops before OS IRP construction and submission',
                                      'Auxiliary handle/device object and outer transport mode are synthetic',
                                      'Target driver identity, blob semantics, authentication and Linux equivalent remain open']}, indent=2))


if __name__ == '__main__':
    main()
