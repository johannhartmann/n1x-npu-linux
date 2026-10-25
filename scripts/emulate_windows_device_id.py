#!/usr/bin/env python3
"""Execute the original Windows hardware-ID classifier with stubbed OS imports."""
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
CTX, IDS, STACK, STUB, STOP = 0x21000000, 0x21100000, 0x21200000, 0x21300000, 0x21300ff0


def run(pe, hardware_id, property_error=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page, size in ((0xd3b000, 0x1000), (0xd57000, 0x1000),
                       (0xed000, 0x1000), (0xcd000, 0x1000), (0xc8000, 0x1000),
                       (0xe3e000, 0x1000), (0xe9f000, 0x2000)):
        u.mem_map(BASE+page, size)
        if page in (0xd3b000, 0xc8000, 0xe9f000):
            u.mem_write(BASE+page, pe.get_data(page, size))
    for address, size in ((CTX, 0x1000), (IDS, 0x1000), (STACK, 0x10000), (STUB, 0x1000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xed2d0, 0xed4e0, 0xcd148):
        u.mem_write(BASE+rva, ret)
    for address in (STUB, STUB+0x10):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3e658, struct.pack('<Q', STUB))
    u.mem_write(BASE+0xe3e240, struct.pack('<Q', STUB+0x10))
    searches = []

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def wide(address):
        data = bytearray()
        for index in range(256):
            word = bytes(u.mem_read(address+index*2, 2))
            if word == b'\0\0':
                return data.decode('utf-16le')
            data.extend(word)
        raise ValueError('unterminated emulator string')

    def hook(_, address, size, user):
        if address == BASE+0xed2d0:
            assert reg(0) == 512
            u.reg_write(regs.UC_ARM64_REG_X0, IDS)
        elif address == BASE+0xcd148:
            u.mem_write(reg(0), bytes(reg(2)))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB:
            # IoGetDeviceProperty(DevicePropertyHardwareID=1).
            assert reg(1) == 1 and reg(2) == 512
            data = (hardware_id+'\0\0').encode('utf-16le')
            assert len(data) <= 512
            u.mem_write(reg(3), data)
            u.mem_write(reg(4), struct.pack('<I', len(data)))
            u.reg_write(regs.UC_ARM64_REG_X0, 0xc0000001 if property_error else 0)
        elif address == STUB+0x10:
            haystack, needle = wide(reg(0)), wide(reg(1))
            searches.append(needle)
            at = haystack.find(needle)
            u.reg_write(regs.UC_ARM64_REG_X0, 0 if at < 0 else reg(0)+at*2)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd3b5c8, STOP, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'chip_id': hex(u.reg_read(regs.UC_ARM64_REG_X0)), 'searched_ids': searches}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path))
    imports = {item.address: item.name for entry in pe.DIRECTORY_ENTRY_IMPORT for item in entry.imports}
    assert imports[BASE+0xe3e658] == b'IoGetDeviceProperty'
    assert imports[BASE+0xe3e240] == b'wcsstr'
    rows = []
    prefix = 'ACPI\\VEN_NVDA&DEV_200A'
    for suffix, expected in (('&REV_0034', '0x254'), ('&REV_2540', '0x254'),
                              ('&REV_0036', '0x256'), ('', '0x0'), ('&REV_9999', '0x0')):
        result = run(pe, prefix+suffix)
        assert result['chip_id'] == expected
        rows.append({'hardware_id': prefix+suffix, **result})
    result = run(pe, prefix+'&REV_0034', property_error=True)
    assert result['chip_id'] == '0x0' and not result['searched_ids']
    rows.append({'case': 'property_read_failure', **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'classifier_rva': '0xd3b5c8', 'cases': rows,
                      'limitations': ['OS hardware-ID retrieval and imported wcsstr are emulated',
                                      'Proves this original classifier, not every alternative Windows construction path',
                                      'Live ACPI table identity and hardware boot remain unverified']}, indent=2))


if __name__ == '__main__':
    main()
