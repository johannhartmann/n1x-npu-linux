#!/usr/bin/env python3
"""Replay global VBIOS gating and later registry initialization in one instance.

OS allocation/resource discovery and ROM acquisition are explicit boundaries.
This checks ordering, not a usable VBIOS-free physical startup configuration.
"""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT
from test_hfrp_vbios import fixture, run as parse_windows

BASE = 0x140000000
OUTER, HAL, GLOBAL, OWNER, SETTINGS, STACK, STUB, DEVICE, POOL = (
    0x3d000000 + i*0x10000 for i in range(9))
STOP = STUB+0xff0


def run(pe, fields, *, override=None, source='fallback', initial_enable=1,
        parse_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd38000, 0x13000), (0x194f000, 0xf000),
                      (0x108e000, 0x2000), (0xe3e000, 0x1000),
                      (0x1c000, 0x1000), (0xd57000, 0x1000),
                      (0x1512000, 0x1000), (0x13ac000, 0x1000),
                      (0xcd000, 0x2000), (0x5000, 0x1000),
                      (0x5ee000, 0x1000), (0x142000, 0x1000),
                      (0x14000, 0x1000), (0xed000, 0x1000),
                      (0xfb000, 0x1000), (0x26c000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (OUTER, HAL, GLOBAL, OWNER, SETTINGS, STACK, STUB, DEVICE, POOL):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)

    def put(address, value, fmt='<I'):
        u.mem_write(address, struct.pack(fmt, value))

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def execute(rva, end=STOP):
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        try:
            u.emu_start(BASE+rva, end, count=100000)
        except Exception as exc:
            raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}; events={events}') from exc
        assert u.reg_read(regs.UC_ARM64_REG_PC) == end

    events = []
    put(OUTER+0x14e, 3, '<H')
    put(HAL+0x496, 1, '<H')
    for address, constructor in ((OUTER, 0x194fb08), (HAL, 0x1951238)):
        u.reg_write(regs.UC_ARM64_REG_X0, address)
        execute(constructor)
    u.reg_write(regs.UC_ARM64_REG_X21, HAL)
    execute(0x195d154, BASE+0x195d1ac)
    assert int.from_bytes(u.mem_read(HAL+0x490, 4), 'little') == 1
    # Only initial_enable=0 is a counterfactual control. It is NOT the effect
    # of a registry override: that read occurs later in the real global path.
    put(HAL+0x490, initial_enable)
    for address, value in ((BASE+0x1512148, GLOBAL), (GLOBAL+0x68, OWNER),
                           (HAL, BASE+0x108e9a0), (HAL+8, BASE+0x108e9b8),
                           (HAL+0x68, HAL+8), (OUTER+0x170, HAL),
                           (OUTER+0x178, HAL+8), (BASE+0xe3eaf0, STUB)):
        put(address, value, '<Q')
    put(BASE+0x13ac13c, 7)
    u.mem_write(SETTINGS, b'\xa5'*0x250)
    u.mem_write(STUB, bytes.fromhex('c0035fd6'))
    chip_getter = int.from_bytes(u.mem_read(OUTER+0x140, 8), 'little')

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        rva = address-BASE
        if rva == 0x143f0:
            ret(0x71)  # OS classifier: runtime device rather than compiler.
        elif rva == 0xd3b5c8 or address == chip_getter:
            ret(0x254)  # Hardware-ID classification is tested separately.
        elif rva == 0x1950ec8:
            assert (reg(1), reg(2), reg(3), reg(4)) == (GLOBAL, 0, 0x254, 0x71)
            put(reg(0), OUTER, '<Q')
            events.append('outer_constructor_boundary')
            ret()
        elif rva == 0xed2d0:
            # Actual bookkeeping and settings constructors allocate these.
            assert reg(0) in (0x18, 0x250)
            ret(POOL if reg(0) == 0x18 else SETTINGS)
        elif rva == 0x195d6d8:
            assert (reg(1), reg(2)) == (GLOBAL, 0)
            events.append('settings_constructor')
            # Execute original constructor, including full 0x250-byte clear.
        elif rva == 0xfbbe8:
            ret(GLOBAL+0x8000)  # OS base-object bookkeeping only.
        elif rva in (0xfbee0, 0x26cbc0, 0x5be0):
            ret()
        elif rva == 0xd3b6b0:
            assert (reg(0), reg(1)) == (OWNER, OUTER)
            events.append('vbios_property_query')
            if source == 'property':
                put(reg(3), POOL+0x100, '<Q')
                put(reg(4), 1)
                ret()
            else:
                ret(0x56)
        elif rva in (0xd3d2c0, 0xd3bab0):
            events.append('vbios_'+('fallback' if rva == 0xd3d2c0 else 'property'))
            assert bytes(u.mem_read(SETTINGS+0x74, 0x190)) == bytes(0x190)
            if not parse_error:
                for offset, value in fields.items():
                    put(SETTINGS+int(offset, 16), value)
            ret(parse_error)
        elif rva == 0xd3b8c0:
            assert (reg(0), reg(1)) == (OWNER, OUTER)
            name = bytes(u.mem_read(reg(2), 80)).split(b'\0')[0].decode()
            if name == 'RmSocDlaEnableVbios':
                events.append('enable_vbios_registry')
                if override is not None:
                    put(reg(3), override)
                    ret()
                    return
            ret(0x56)
        elif rva in (0xd49e20, 0xd43730, 0xd41b50, 0xd405f0, 0xd439c0, 0xd41db0):
            name = {0xd49e20: 'remapper', 0xd43730: 'buffer_setup',
                    0xd41b50: 'hfrp', 0xd405f0: 'power',
                    0xd439c0: 'loader', 0xd41db0: 'post_start'}[rva]
            events.append(name)
            if rva == 0xd405f0:
                assert (reg(0), reg(1), reg(2)) == (OUTER, 0, 7)
            ret()
        elif rva in (0xd3b530, 0xd3b1d8, 0xfbce0, 0xed4e0, 0xce4b0,
                      0xd38a38, 0xd38758):
            ret()
        elif rva == 0xcd148:
            assert reg(1) == 0 and reg(2) <= 0x250
            u.mem_write(reg(0), bytes(reg(2)))
            ret()

    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in enumerate((GLOBAL, DEVICE, 0, 0)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    execute(0xd38a88)
    return {'status': reg(0), 'events': events,
            'vbios_enabled': int.from_bytes(u.mem_read(HAL+0x490, 4), 'little'),
            'settings_pointer': int.from_bytes(u.mem_read(GLOBAL+0x78, 8), 'little'),
            'gating_bytes': list(u.mem_read(OUTER+0x31d, 9)),
            'request_mask': int.from_bytes(u.mem_read(OUTER+0x4b0, 4), 'little')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    fields = parse_windows(pe, fixture())['fields']
    fields.update({'0x80': 0, '0xa0': 0, '0xec': 0})
    rows = []
    for source in ('fallback', 'property'):
        for override in (None, 0, 1, 2, 0xffffffff):
            result = run(pe, fields, source=source, override=override)
            assert result['status'] == 0 and result['settings_pointer'] == SETTINGS, result
            assert result['vbios_enabled'] == int(override is None or override == 1), result
            assert result['events'].index('vbios_'+source) < result['events'].index('enable_vbios_registry'), result
            assert result['events'][-4:] == ['hfrp', 'power', 'loader', 'post_start'], result
            assert result['request_mask'] == 7, result
            # Registry zero does not suppress earlier-populated gating fields.
            assert result['gating_bytes'][2] == fields['0xf8'] & 255, result
            assert result['gating_bytes'][5] == fields['0xfc'] & 255, result
            rows.append({'source': source, 'override': override, **result})
    changed = run(pe, dict(fields, **{'0xa0': 1, '0xec': 1}), override=0)
    assert changed['vbios_enabled'] == 0 and changed['request_mask'] == 0x777, changed
    rejected = run(pe, dict(fields, **{'0x80': 1}), override=0)
    assert rejected['status'] == 0x40 and 'enable_vbios_registry' not in rejected['events'], rejected
    assert 'loader' not in rejected['events'] and rejected['settings_pointer'] == 0, rejected
    control = run(pe, fields, initial_enable=0, override=0)
    assert control['status'] == 0 and control['settings_pointer'] == 0, control
    assert not any(e.startswith('vbios_') for e in control['events']), control
    assert control['gating_bytes'] == [0]*9 and control['request_mask'] == 7, control
    errors = []
    for source in ('fallback', 'property'):
        failed = run(pe, fields, source=source, override=0, parse_error=0x65)
        assert failed['status'] == 0 and 'loader' in failed['events'], failed
        assert failed['gating_bytes'] == [1, 1, 1, 1, 1, 3, 1, 1, 15], failed
        errors.append({'source': source, **failed})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'kmd_sha256': KMD_SHA256, 'cases': rows,
                      'disabled_registry_still_uses_policy': changed,
                      'feature_rejected_before_registry': rejected,
                      'counterfactual_initial_gate_zero': control,
                      'ignored_acquisition_errors': errors,
                      'limitations': [
                          'Outer allocation and OS discovery are reconstructed boundaries; settings constructor executes',
                          'ROM acquisition supplies synthetic fields from the separately checked parser',
                          'Remapper, buffer setup, HFRP, power, loader and post-start are stubs',
                          'No physical VBIOS-free startup, firmware execution or inference is established']}, indent=2))


if __name__ == '__main__':
    main()
