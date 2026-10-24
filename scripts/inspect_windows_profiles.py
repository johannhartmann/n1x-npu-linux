#!/usr/bin/env python3
"""Execute original Windows profile selection and resource-list providers in RAM.

This establishes internal profile chains, not which physical aperture is safe
on the live P16. It deliberately never maps any reported physical address.
"""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, OUT, STACK, STOP = 0x20000000, 0x20100000, 0x20200000, 0x20300000


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0x194f000, 0x2000), (BASE+0xd3c000, 0x1000),
                       (BASE+0xd3f000, 0x1000), (BASE+0xd42000, 0x1000),
                       (BASE+0xd39000, 0x1000), (BASE+0x16000, 0x1000),
                       (BASE+0x13ac000, 0x1000), (BASE+0xe3e000, 0x1000),
                       (CTX, 0x1000), (OUT, 0x1000), (STACK, 0x10000), (STOP, 0x1000)):
        uc.mem_map(addr, size)
        if addr < CTX or addr >= BASE:
            uc.mem_write(addr, pe.get_data(addr-BASE, size))
    # CFG validation is an OS mechanism, stubbed; actual function is still run.
    uc.mem_write(STOP+0x100, bytes.fromhex('c0035fd6'))
    uc.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STOP+0x100))

    def call(address, args=(), until=STOP, extra=()):
        for i, value in enumerate(args):
            uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        for i, value in extra:
            uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
        uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
        uc.emu_start(address, until, count=10000)
        assert uc.reg_read(regs.UC_ARM64_REG_PC) == until
        return uc.reg_read(regs.UC_ARM64_REG_X0)

    rows = []
    for chip_id, profile, dla_input, dla_selector in (
            (0x254, 3, 0x303033, 1), (0x256, 4, 0x303033, 1),
            (0x354, 6, 0x404044, 2)):
        uc.mem_write(CTX, bytes(0x1000))
        call(BASE+0xd3c0e8, (CTX+0x14e, chip_id))
        assert struct.unpack('<H', uc.mem_read(CTX+0x14e, 2))[0] == profile
        call(BASE+0x194fb08, (CTX,))
        getter = struct.unpack('<Q', uc.mem_read(CTX+0x140, 8))[0]
        assert call(getter) == chip_id
        resource_init = struct.unpack('<Q', uc.mem_read(CTX+0x128, 8))[0]
        provider = struct.unpack('<Q', uc.mem_read(CTX+0x138, 8))[0]
        assert resource_init == BASE+0xd3fd80
        assert call(provider, (CTX, OUT, OUT+8)) == 0
        pointer = struct.unpack('<Q', uc.mem_read(OUT, 8))[0]
        count = struct.unpack('<I', uc.mem_read(OUT+8, 4))[0]
        assert count in (2, 3)
        resources = []
        for i in range(count):
            ident, reserved, address, length, tail = struct.unpack('<IIQII', uc.mem_read(pointer+i*24, 24))
            assert reserved == tail == 0
            resources.append({'id': ident, 'physical': hex(address), 'bytes': hex(length)})
        for i in range(8):
            uc.mem_write(CTX+0x1a8+i*0x30, struct.pack('<I', i))
        call(BASE+0xd39508, until=BASE+0xd395bc,
             extra=((19, CTX), (20, CTX), (23, 0x71), (26, CTX+0x1a8)))
        observed = uc.reg_read(regs.UC_ARM64_REG_X3)
        assert observed == dla_input
        call(BASE+0xd3c8d0, (OUT, observed))
        assert struct.unpack('<H', uc.mem_read(OUT, 2))[0] == dla_selector
        rows.append({'input_chip_id': hex(chip_id), 'device_profile': profile,
                     'device_profile_setter_rva': '0xd3c0e8',
                     'getter_rva': hex(getter-BASE), 'provider_rva': hex(provider-BASE),
                     'resource_list_rva': hex(pointer-BASE), 'resources': resources,
                     'dla_constructor_input': hex(observed), 'dla_hal_selector': dla_selector})
    assert rows[2]['resources'] == [
        {'id': 0, 'physical': '0x19340000', 'bytes': '0x40000'},
        {'id': 1, 'physical': '0x3a340000', 'bytes': '0x40000'},
        {'id': 2, 'physical': '0x31380000', 'bytes': '0x10000'}]
    print(json.dumps({'scope': 'Original ARM64 classifiers, HAL initializer, getters and branch slice in RAM',
                      'profiles': rows, 'limitations': [
                          'The live machine supplies no verified mapping from ACPI HRV to Windows input_chip_id',
                          'The 0x354 resource list differs from the historical P16 DSDT',
                          'These are candidate physical addresses, not authorization or proof for MMIO access']}, indent=2))


if __name__ == '__main__':
    main()
