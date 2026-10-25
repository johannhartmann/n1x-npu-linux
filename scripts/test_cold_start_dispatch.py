#!/usr/bin/env python3
"""Replay original cold-start dispatch; hardware/policy bodies are explicit stubs."""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
OUTER, HAL, STACK, STUB = (0x39000000+i*0x10000 for i in range(4))
STOP = STUB+0xff0


def run(pe, entry, *, phase=0, fail=None, runtime=True, target=None,
        policy_mask=0, notify=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for start, size in ((0xd38000, 0x13000), (0x194f000, 0xf000),
                        (0x108e000, 0x2000), (0xe3e000, 0x1000),
                        (0x5ee000, 0x1000), (0x142000, 0x1000),
                        (0x5000, 0x1000), (0xce000, 0x1000)):
        u.mem_map(BASE+start, size)
        u.mem_write(BASE+start, pe.get_data(start, size))
    for address in (OUTER, HAL, STACK, STUB):
        u.mem_map(address, 0x10000)

    def put(address, value, fmt='<Q'):
        u.mem_write(address, struct.pack(fmt, value))

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def execute(address):
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
        u.emu_start(BASE+address, STOP, count=50000)
        assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP

    # Actual profile-3 and HAL-1 callback constructors.
    put(OUTER+0x14e, 3, '<H')
    put(HAL+0x496, 1, '<H')
    for address, constructor in ((OUTER, 0x194fb08), (HAL, 0x1951238)):
        u.reg_write(regs.UC_ARM64_REG_X0, address)
        execute(constructor)
    # Object bookkeeping from 195d0fc..195d134 and d3962c..d39690.
    # Allocation/base-object construction is not executed by this test.
    put(HAL, BASE+0x108e9a0)
    put(HAL+8, BASE+0x108e9b8)
    put(HAL+0x68, HAL+8)
    put(OUTER+0x170, HAL)
    put(OUTER+0x178, HAL+8 if runtime else 0)
    put(OUTER+0x310, phase, '<I')
    put(OUTER+0x314, 0x12345678, '<I')
    put(OUTER+0x318, 0xabcdef01, '<I')
    put(OUTER+0x4b0, policy_mask, '<I')
    put(OUTER+0x14a, int(notify), '<B')
    assert int.from_bytes(u.mem_read(OUTER+0xf8, 8), 'little') == BASE+0xd40d50
    put(BASE+0xe3eaf0, STUB)
    u.mem_write(STUB, bytes.fromhex('c0035fd6'))
    boundaries = {0xd440d0: 'registry', 0xd41b50: 'hfrp_startup',
                  0xd44710: 'gating_policy', 0xd43730: 'resume_setup',
                  0xd405f0: 'power_on', 0xd439c0: 'loader'}
    cleanup = int.from_bytes(u.mem_read(HAL+0xd0, 8), 'little')-BASE
    assert cleanup not in boundaries
    boundaries[cleanup] = 'cleanup'
    if target is not None:
        # Stop/notification internals are outside this transition replay.
        boundaries[0xd39de0] = 'stop'
        boundaries[0xd40e90] = 'notify'
    events, observations = [], []

    def hook(_, address, size, user):
        rva = address-BASE
        if rva in boundaries:
            name = boundaries[rva]
            assert reg(0) == OUTER, (name, hex(reg(0)))
            if name in ('registry', 'gating_policy', 'resume_setup', 'loader', 'cleanup'):
                assert reg(1) == HAL, (name, hex(reg(1)))
            if name == 'power_on':
                assert reg(1) == 0 and reg(2) in ((7,) if target is None else (1, 7, 16))
            observations.append({'event': name, 'state': list(struct.unpack('<III', u.mem_read(OUTER+0x310, 12))),
                                 'power_mask': reg(2) if name == 'power_on' else None})
            events.append(name)
            u.reg_write(regs.UC_ARM64_REG_X0, 0x65 if name == fail else 0)
            u.reg_write(regs.UC_ARM64_REG_PC, reg(30))
        elif rva in (0xd38a38, 0xd38758, 0xce4b0):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
            u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    if target is not None:
        assert entry == 0xd40d50
        u.reg_write(regs.UC_ARM64_REG_X1, target)
    execute(entry)
    return {'entry': hex(entry), 'phase': phase, 'runtime': runtime,
            'fail': fail, 'status': reg(0), 'events': events,
            'state': list(struct.unpack('<III', u.mem_read(OUTER+0x310, 12))),
            'observations': observations,
            'loader_stage': int.from_bytes(u.mem_read(HAL+0x524, 4), 'little')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for entry, phase, expected in (
        (0xd39b98, 0, ['registry']),
        (0xd39d38, 0, ['hfrp_startup', 'gating_policy']),
        (0xd39c08, 0, ['power_on', 'loader']),
        (0xd39c08, 3, ['hfrp_startup', 'resume_setup', 'power_on', 'loader']),
    ):
        for fail in (None, *expected):
            result = run(pe, entry, phase=phase, fail=fail)
            events = expected if fail is None else expected[:expected.index(fail)+1]
            if fail in ('power_on', 'loader'):
                events = events+['cleanup']
            assert result['events'] == events, result
            assert result['status'] == (0 if fail is None else 0x65), result
            assert result['loader_stage'] == (4 if 'loader' in events else 3 if 'power_on' in events else 0), result
            rows.append(result)
    for entry, expected in ((0xd39b98, []), (0xd39d38, ['hfrp_startup']),
                            (0xd39c08, [])):
        result = run(pe, entry, runtime=False)
        assert result['events'] == expected and result['status'] == 0, result
        rows.append(result)
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'kmd_sha256': KMD_SHA256, 'cases': rows,
                      'limitations': [
                          'Object bookkeeping is reconstructed; callback constructors and dispatch execute unchanged',
                          'Registry, HFRP setup, gating/resume policy, power command, loader and cleanup bodies are stubbed',
                          'Global initializer is not replayed; the state writer has a separate test_dla_power_state replay',
                          'No physical firmware boot, DMA or power transition is tested']}, indent=2))


if __name__ == '__main__':
    main()
