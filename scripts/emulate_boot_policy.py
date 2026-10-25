#!/usr/bin/env python3
"""Run intermediate Windows boot/default registry slices in RAM.

Remapper slice outputs are later overwritten by the complete initializer;
see test_hfrp_policy_init.py for final startup values.
"""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE, CTX, STACK, STUB = 0x140000000, 0x21000000, 0x21100000, 0x21200000


def run(pe, selector, override=None, remapper_initial=None):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page, size in ((0x1951000, 0xd000), (0xd44000, 0x1000), (0xd3b000, 0x1000),
                       (0x1c000, 0x1000), (0xe3e000, 0x1000), (0x108f000, 0x1000)):
        u.mem_map(BASE+page, size)
        if page != 0xd3b000:
            u.mem_write(BASE+page, pe.get_data(page, size))
    for address, size in ((CTX, 0x1000), (STACK, 0x10000), (STUB, 0x1000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    u.mem_write(BASE+0xd3b8c0, ret)
    u.mem_write(STUB, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(CTX+0x496, struct.pack('<H', selector))
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_LR, STUB+0xff0)
    u.emu_start(BASE+0x1951238, STUB+0xff0, count=50000)
    getter = struct.unpack('<Q', u.mem_read(CTX+0x410, 8))[0]
    assert getter == BASE+0x1c490
    queried = []

    def hook(_, address, size, user):
        if address == BASE+0xd3b8c0:
            name_address = u.reg_read(regs.UC_ARM64_REG_X2)
            name = bytes(u.mem_read(name_address, 64)).split(b'\0')[0].decode()
            queried.append(name)
            if override is not None:
                u.mem_write(u.reg_read(regs.UC_ARM64_REG_X3), struct.pack('<I', override))
            u.reg_write(regs.UC_ARM64_REG_X0, 0x56 if override is None else 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X19, CTX), (regs.UC_ARM64_REG_X20, CTX),
                            (regs.UC_ARM64_REG_X21, CTX), (regs.UC_ARM64_REG_X22, 1),
                            (regs.UC_ARM64_REG_SP, STACK+0x8000)):
        u.reg_write(register, value)
    u.mem_write(CTX+0x460, struct.pack('<I', 0x77))
    # Execute the complete contiguous default-field sequence, not just one STRB.
    u.mem_write(CTX+0x478, struct.pack('<I', 0x77))
    u.mem_write(CTX+0x486, b'\x77')
    u.emu_start(BASE+0x195d154, BASE+0x195d1ac, count=100)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x195d1ac
    defaults = {hex(offset): struct.unpack('<I', u.mem_read(CTX+offset, 4))[0]
                for offset in (0x470, 0x478, 0x480, 0x488, 0x490)}
    assert defaults == {'0x470': 1, '0x478': 1, '0x480': 1, '0x488': 0, '0x490': 1}
    assert bytes(u.mem_read(CTX+0x486, 1)) == b'\0'
    if remapper_initial is not None:
        u.mem_write(CTX+0x478, struct.pack('<I', remapper_initial))
    initial = struct.unpack('<I', u.mem_read(CTX+0x478, 4))[0]
    u.emu_start(BASE+0xd44130, BASE+0xd44168, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0xd44168
    vbios_enabled = struct.unpack('<I', u.mem_read(CTX+0x490, 4))[0]
    u.emu_start(BASE+0xd4419c, BASE+0xd44208, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0xd44208
    method = struct.unpack('<I', u.mem_read(CTX+0x460, 4))[0]
    u.emu_start(BASE+0xd44284, BASE+0xd442b8, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0xd442b8
    reinit_mode = bytes(u.mem_read(CTX+0x486, 1))[0]
    u.emu_start(BASE+0xd44310, BASE+0xd44350, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0xd44350
    remapper = struct.unpack('<I', u.mem_read(CTX+0x478, 4))[0]
    return {'selector': selector, 'override': override, 'method': method,
            'vbios_enabled': vbios_enabled,
            'sroot_reinit_mode_byte': reinit_mode, 'constructor_defaults': defaults, 'initial_remapper': initial, 'remapper': remapper, 'queries': queried}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = []
    for selector in (1, 2):
        for override in (None, 0, 1, 2, 3, 0xffffffff):
            result = run(pe, selector, override)
            assert result['method'] == (2 if override is None else override if override in (1, 2) else 0)
            assert result['remapper'] == (1 if override is None else override if override in (0, 2) else 1)
            assert result['sroot_reinit_mode_byte'] == int(override == 1)
            assert result['vbios_enabled'] == int(override is None or override == 1)
            assert result['queries'] == ['RmSocDlaEnableVbios', 'RmSocDlaFwLoadMethod',
                                         'RmSocDlaSrootReinitMode', 'RmSocDlaRemapper']
            cases.append(result)
    for initial in (0, 2, 0x77):
        result = run(pe, 1, remapper_initial=initial)
        assert result['remapper'] == initial and result['method'] == 2
        cases.append(result)
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'cases': cases, 'limitations': ['Registry reads are stubbed; actual installed Windows values are unknown',
                                                     'Remapper slice outputs are intermediate: full initialization overwrites them later',
                                                     'Default-field constructor slice executes; allocation and earlier object setup are not replayed',
                                                     'No loader, firmware transport or hardware boot is executed']}, indent=2))


if __name__ == '__main__':
    main()
