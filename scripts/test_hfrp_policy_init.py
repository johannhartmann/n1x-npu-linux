#!/usr/bin/env python3
"""Replay complete Windows policy initializer and gating-mask producer in RAM."""
import hashlib
import json
import struct
import ctypes
import errno
import os
from pathlib import Path
import random
import shlex
import subprocess
import tempfile

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT
from test_hfrp_vbios import Policy, fixture, run as parse_windows

BASE = 0x140000000
OUTER, HAL, GLOBAL, OWNER, SETTINGS, STACK, STUB = (
    0x3c000000+i*0x10000 for i in range(7))
STOP = STUB+0xff0


class Gating(ctypes.Structure):
    _fields_ = [('request_mask', ctypes.c_uint32), ('policy_bytes', ctypes.c_uint8*9),
                ('slcg_override', ctypes.c_uint8), ('hal_flags', ctypes.c_uint8*4)]


def compare_native(pe, fields):
    with tempfile.TemporaryDirectory(prefix='n1x-gating-') as tmp:
        library = Path(tmp)/'gating.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_vbios.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    lib.n1x_dla_prepare_gating.argtypes = [ctypes.POINTER(Policy), ctypes.POINTER(Gating)]
    lib.n1x_dla_prepare_gating.restype = ctypes.c_int
    rng = random.Random(0x200a)
    cases = 0
    # Every individual feature except rejected bit 0, opposing bits together,
    # zero/max byte values and arbitrary combinations exercise update ordering.
    masks = [0, 0xfffffffe]+[1 << bit for bit in range(1, 32)]+[rng.getrandbits(32) & ~1 for _ in range(32)]
    for enabled in (0, 255):
        for mask in masks:
            policy, result = Policy(), Gating()
            policy.boot_tables_valid = 1
            policy.feature_mask = mask
            policy.frequency_flags[0] = enabled
            policy.gating_flags = enabled
            policy.gating_bytes[:] = [0, rng.choice((0, 1, 255)), rng.choice((0, 3, 255))]
            original = run(pe, dict(fields, **{
                '0x80': mask, '0xa0': enabled, '0xec': enabled,
                '0xf4': policy.gating_bytes[0], '0xf8': policy.gating_bytes[1],
                '0xfc': policy.gating_bytes[2]}))
            before = bytes(policy)
            assert lib.n1x_dla_prepare_gating(ctypes.byref(policy), ctypes.byref(result)) == 0
            assert bytes(policy) == before
            assert result.request_mask == original['mask']
            assert list(result.policy_bytes) == original['gating_bytes']
            assert result.slcg_override == original['slcg_override']
            assert list(result.hal_flags) == list(original['hal_flags'].values())
            cases += 1
    failures = 0
    for invalid, expected in (('input', -errno.EINVAL), ('output', -errno.EINVAL),
                              ('partial', -errno.EINVAL), ('valid_flag', -errno.EINVAL),
                              ('feature0', -errno.EOPNOTSUPP)):
        policy.boot_tables_valid = 1
        policy.feature_mask = 0
        if invalid == 'partial': policy.boot_tables_valid = 0
        if invalid == 'valid_flag': policy.boot_tables_valid = 2
        if invalid == 'feature0': policy.feature_mask = 1
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result), bytes(policy)
        assert lib.n1x_dla_prepare_gating(None if invalid == 'input' else ctypes.byref(policy),
            None if invalid == 'output' else ctypes.byref(result)) == expected
        assert (bytes(result), bytes(policy)) == before
        failures += 1
    return {'original_vs_native_cases': cases, 'rejected_inputs': failures}


def run(pe, fields, *, overrides=None, disabled=False, setup_error=0):
    overrides = overrides or {}
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for start, size in ((0xd38000, 0x13000), (0x194f000, 0xf000),
                        (0x108e000, 0x2000), (0xe3e000, 0x1000),
                        (0x1c000, 0x1000), (0xd57000, 0x1000),
                        (0x1512000, 0x1000), (0x13ac000, 0x1000),
                        (0xcd000, 0x2000), (0x5000, 0x1000)):
        u.mem_map(BASE+start, size)
        u.mem_write(BASE+start, pe.get_data(start, size))
    for address in (OUTER, HAL, GLOBAL, OWNER, SETTINGS, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)

    def put(address, value, fmt='<I'):
        u.mem_write(address, struct.pack(fmt, value))

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def execute(start, end=STOP):
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
        try:
            u.emu_start(BASE+start, end, count=100000)
        except Exception as exc:
            raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
        assert u.reg_read(regs.UC_ARM64_REG_PC) == end

    put(OUTER+0x14e, 3, '<H')
    put(HAL+0x496, 1, '<H')
    for address, constructor in ((OUTER, 0x194fb08), (HAL, 0x1951238)):
        u.reg_write(regs.UC_ARM64_REG_X0, address)
        execute(constructor)
    u.reg_write(regs.UC_ARM64_REG_X21, HAL)
    execute(0x195d154, BASE+0x195d1ac)
    for address, value in ((BASE+0x1512148, GLOBAL), (GLOBAL+0x68, OWNER),
                           (GLOBAL+0x78, SETTINGS if fields is not None else 0),
                           (HAL, BASE+0x108e9a0), (BASE+0xe3eaf0, STUB)):
        put(address, value, '<Q')
    put(BASE+0x13ac13c, 7)
    put(OWNER+0x58, int(disabled), '<B')
    if fields is not None:
        for offset, value in fields.items():
            put(SETTINGS+int(offset, 16), value)
    u.mem_write(STUB, bytes.fromhex('c0035fd6'))
    chip_getter = int.from_bytes(u.mem_read(OUTER+0x140, 8), 'little')
    events, queries = [], []

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        rva = address-BASE
        if rva == 0xd3b8c0:
            assert (reg(0), reg(1)) == (OWNER, OUTER)
            name = bytes(u.mem_read(reg(2), 80)).split(b'\0')[0].decode()
            queries.append(name)
            if name in overrides:
                put(reg(3), overrides[name])
                ret()
            else:
                ret(0x56)
        elif address == chip_getter:
            ret(0x254)
        elif rva in (0xd49e20, 0xd43730):
            assert (reg(0), reg(1)) == (OUTER, HAL)
            events.append({'boundary': hex(rva), 'remapper': int.from_bytes(u.mem_read(HAL+0x478, 4), 'little')})
            ret(setup_error if rva == 0xd43730 else 0)
        elif rva == 0xcd148:
            assert reg(1) == 0 and reg(2) <= 0x80
            u.mem_write(reg(0), bytes(reg(2)))
            ret()
        elif rva == 0xce4b0:
            ret()

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    u.reg_write(regs.UC_ARM64_REG_X1, HAL)
    execute(0xd440d0)
    status = reg(0)
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    execute(0xd44710)
    return {'status': status, 'mask': int.from_bytes(u.mem_read(OUTER+0x4b0, 4), 'little'),
            'gating_bytes': list(u.mem_read(OUTER+0x31d, 9)),
            'slcg_override': u.mem_read(OUTER+0x14c, 1)[0],
            'hal_flags': {hex(i): u.mem_read(HAL+i, 1)[0] for i in (0x47d, 0x47e, 0x484, 0x485)},
            'hal': {hex(i): int.from_bytes(u.mem_read(HAL+i, 4), 'little') for i in (0x460, 0x478, 0x490, 0x590, 0x594, 0x5a0, 0x5a4, 0x5a8)},
            'settings': {hex(i): int.from_bytes(u.mem_read(SETTINGS+i, 4), 'little') for i in (0xf4, 0xf8, 0xfc)},
            'events': events, 'queries': queries}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    fields = parse_windows(pe, fixture())['fields']
    fields['0x80'] = 0
    rows = []
    for freq in (0, 1):
        for gating in (0, 1):
            for disabled in (False, True):
                values = dict(fields, **{'0xa0': freq, '0xec': gating, '0xf4': 0, '0xf8': 0, '0xfc': 0})
                result = run(pe, values, disabled=disabled)
                assert result['status'] == 0
                assert result['mask'] == (0 if disabled else 7 | (0x110 if freq else 0) | (0x660 if gating else 0)), result
                assert result['gating_bytes'] == [int(not freq), 1, 1, int(not gating), 1, 3, int(not gating), 1, 15], result
                assert result['settings'] == {'0xf4': 4, '0xf8': 1, '0xfc': 3}
                assert result['hal']['0x590'] == fields['0x94'] and result['hal']['0x594'] == fields['0xbc']
                assert [result['hal'][hex(i)] for i in (0x5a0, 0x5a4, 0x5a8)] == [1, 4, 10]
                rows.append({'frequency_flag': freq, 'gating_flag': gating, 'disabled': disabled, **result})
    # Compare single feature bits from VBIOS and registry across complete init.
    features = []
    for enabled in (False, True):
        base = dict(fields, **{'0xa0': int(enabled), '0xec': int(enabled)})
        for bit in range(32):
            vbios = run(pe, dict(base, **{'0x80': 1 << bit}))
            registry = run(pe, base, overrides={'RmSocDlaFeature': 1 << bit})
            for name in ('mask', 'gating_bytes', 'slcg_override', 'hal', 'hal_flags'):
                assert vbios[name] == registry[name], (bit, name)
            expected = 0x777 if enabled else 7
            expected &= ~{1: 0x110, 3: 0x660, 4: 0x440}.get(bit, 0)
            expected |= {17: 0x110, 19: 0x220, 20: 0x660}.get(bit, 0)
            assert vbios['mask'] == expected, (bit, vbios)
            assert vbios['slcg_override'] == int(bit == 2)
            features.append({'bit': bit, 'base_enabled': enabled, 'mask': vbios['mask'],
                             'gating_bytes': vbios['gating_bytes'], 'slcg_override': vbios['slcg_override'],
                             'hal_flags': vbios['hal_flags']})
    # A later registry feature pass acts after VBIOS, rather than ORing masks.
    for source, override, expected in ((1 << 17, 1 << 1, 0x667),
                                       (1 << 1, 1 << 17, 0x777),
                                       ((1 << 1) | (1 << 17), 0, 0x777)):
        result = run(pe, dict(fields, **{'0x80': source}), overrides={'RmSocDlaFeature': override})
        assert result['mask'] == expected, result
    for value in (0, 1, 2, 0xffffffff):
        result = run(pe, fields, overrides={'RmSocDlaRemapper': value})
        assert result['hal']['0x478'] == 1, result
    error = run(pe, fields, setup_error=0x65)
    assert error['status'] == 0x65
    absent = run(pe, None)
    assert absent['mask'] == 7 and absent['gating_bytes'] == [0]*9
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'native_comparison': compare_native(pe, fields),
                      'policy_cases': rows, 'feature_bits': features,
                      'remapper_overrides_final_value': 1, 'setup_error_propagated': error['status'],
                      'limitations': ['HAL remapper/setup callbacks are stubbed; no resource allocation or device I/O',
                                      'Global settings and owner are synthetic; VBIOS fields originate in the separately replayed parser',
                                      'Single feature bits are compared, but hardware meanings are not inferred',
                                      'Missing settings case is observed Windows behavior, not an approved fallback']}, indent=2))


if __name__ == '__main__':
    main()
