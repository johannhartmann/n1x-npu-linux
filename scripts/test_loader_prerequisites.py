#!/usr/bin/env python3
"""Replay Windows pre-loader policy; HFRP/loader/OS boundaries are stubs."""
import hashlib
import json
import re
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs

from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
OUTER, HAL, HFRP, PROVIDER, LINK, STACK, STUB = (
    0x30000000 + i * 0x10000 for i in range(7))
STOP = STUB + 0xff0


def run(pe, mode=0, capability=1, absent=None, guard=0, pending=False,
        pending_error=0, transport_error=0, firmware_error=0, loader_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd41000, 0x3000), (0xd3f000, 0x1000),
                      (0xd3d000, 0x2000), (0xd38000, 0x1000),
                      (0xd57000, 0x1000),
                      (0xcd000, 0x2000), (0x13ac000, 0x1000),
                      (0xe3e000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (OUTER, HAL, HFRP, PROVIDER, LINK, STACK, STUB):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd3de30, 0xd439c0, 0xd38a38, 0xd38758, 0xce4b0, 0xcd148):
        u.mem_write(BASE+rva, ret)
    u.mem_write(STUB, ret)
    u.mem_write(STUB+0x10, ret)
    for address, value in ((BASE+0xe3eaf0, STUB), (OUTER+0xe0, STUB+0x10),
                           (OUTER+0x170, HAL), (OUTER+0x300, 0 if absent == 'outer' else HFRP),
                           (HFRP+0x60, 0 if absent == 'provider' else PROVIDER),
                           (PROVIDER+0x18, 0 if absent == 'link' else LINK)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(LINK+8, struct.pack('<I', capability))
    u.mem_write(OUTER+0x4a8, struct.pack('<I', int(pending)))
    events = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xcd148:
            assert reg(2) == 1 and reg(1) == 0
            u.mem_write(reg(0), b'\0')
        elif address == BASE+0xd3de30:
            assert reg(0) == HFRP and reg(3) == 1
            events.append({'command': hex(reg(1)), 'payload': bytes(u.mem_read(reg(2), 1)).hex()})
            u.mem_write(reg(4), struct.pack('<H', firmware_error))
            u.mem_write(reg(5), b'\0')
            u.mem_write(reg(6), struct.pack('<I', 0))
            u.reg_write(regs.UC_ARM64_REG_X0, transport_error)
        elif address == BASE+0xd439c0:
            assert (reg(0), reg(1)) == (OUTER, HAL)
            events.append({'call': 'common_loader'})
            u.reg_write(regs.UC_ARM64_REG_X0, loader_error)
        elif address == STUB+0x10:
            assert reg(0) == OUTER
            events.append({'call': 'pending_callback'})
            u.reg_write(regs.UC_ARM64_REG_X0, pending_error)
        elif address in (BASE+0xd38a38, BASE+0xd38758, BASE+0xce4b0):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X0, OUTER), (regs.UC_ARM64_REG_X1, guard),
                            (regs.UC_ARM64_REG_X2, mode), (regs.UC_ARM64_REG_SP, STACK+0x8000),
                            (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(register, value)
    try:
        u.emu_start(BASE+0xd41740, STOP, count=100000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'events': events}


def check_noop(pe):
    """Run the original selector-1 constructor and its +0x3d8 target."""
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x1951000, 0xd000), (0x5000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    u.mem_map(HAL, 0x10000)
    u.mem_write(HAL+0x496, struct.pack('<H', 1))
    u.reg_write(regs.UC_ARM64_REG_X0, HAL)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x1951238, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    target = struct.unpack('<Q', u.mem_read(HAL+0x3d8, 8))[0]
    assert target == BASE+0x5be0
    parent_entries = {hex(slot): hex(struct.unpack('<Q', u.mem_read(HAL+slot, 8))[0]-BASE)
                      for slot in (0xc8, 0x210)}
    assert parent_entries == {'0xc8': '0xd47700', '0x210': '0xd48b30'}
    assert bytes(u.mem_read(target, 8)) == bytes.fromhex('00008052c0035fd6')
    u.reg_write(regs.UC_ARM64_REG_X0, HAL)
    u.reg_write(regs.UC_ARM64_REG_X1, 1)
    u.emu_start(target, STOP, count=10)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert u.reg_read(regs.UC_ARM64_REG_X0) == 0
    return {'slot': '0x3d8', 'target_rva': '0x5be0', 'instructions': ['mov w0, 0', 'ret'],
            'parent_entries': parent_entries}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    reference = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/port/pm/nvdla_pm_hfrp_cmd_v1_3.c'
    data = reference.read_bytes()
    assert hashlib.sha256(data).hexdigest() == '22d224fc2ad2ccf0db5a7c0e2ae72f048a16dd5f49b261aa25bfad64075e8acf'
    for name, value in (('RESET', 0x19a), ('RESET_IDLE', 0x19b), ('RESET_SZ', 1), ('RESET_IDLE_SZ', 1)):
        match = re.search(r'^#define DLA_HFRP_CMD_'+name+r'\s+(\d+)U?\s*$', data.decode(), re.MULTILINE)
        assert match and int(match[1]) == value
    rows = []

    def check(inputs, expected, status=0):
        result = run(pe, **inputs)
        assert result == {'status': status, 'events': expected}, (inputs, result)
        rows.append({'inputs': inputs, **result})

    loader = {'call': 'common_loader'}
    baseline = None
    for mode, first, last in ((0, '03', '0c'), (1, '00', '04'), (2, '02', '08'),
                               (3, '01', '00'), (4, '00', '00'), (256, '03', '0c')):
        events = [{'command': '0x19b', 'payload': first},
                  {'command': '0x19a', 'payload': '01'},
                  {'command': '0x19b', 'payload': last}, loader]
        check({'mode': mode}, events)
        if mode == 0:
            baseline = events
    for capability in (0, 2):
        check({'capability': capability}, [loader])
    for absent in ('outer', 'provider', 'link'):
        check({'absent': absent}, [loader])
    check({'guard': 1}, [], 0x56)
    check({'pending': True}, [{'call': 'pending_callback'}]+baseline)
    check({'pending': True, 'pending_error': 0x65}, [{'call': 'pending_callback'}], 0x65)
    check({'transport_error': 0x65}, baseline)
    check({'firmware_error': 0x1234}, baseline)
    check({'loader_error': 0x65}, baseline, 0x65)
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'selector1_preupload_callback': check_noop(pe), 'cases': rows,
                      'limitations': ['Original parent 0xd41740 and helper 0xd3f120 execute; HFRP and common loader are stubs',
                                      'Synthetic object state; live Windows path selection is unknown',
                                      'HFRP command effects, safe physical ordering and P16 failure causality are unproven']}, indent=2))


if __name__ == '__main__':
    main()
