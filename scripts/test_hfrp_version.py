#!/usr/bin/env python3
"""Recover Windows HFRP version selection; handshake/MMIO/transport are stubs."""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
HFRP, PROVIDER, LINK, OWNER, STACK, STUB = (0x35000000+i*0x10000 for i in range(6))
STOP = STUB+0xff0


def run(pe, stamp, *, capability=1, handshake_error=0, read_error=0,
        query_error=0, firmware_error=0, response=0x00010500, response_size=4):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd3d000, 0xd4a000, 0xd4b000, 0xd39000, 0xd38000,
                 0xd57000, 0xcd000, 0xce000, 0xed000, 0xee000, 0x13ac000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (HFRP, PROVIDER, LINK, OWNER, STACK, STUB):
        u.mem_map(address, 0x10000)
    for address, value in ((HFRP+0x60, PROVIDER), (PROVIDER+0x18, LINK), (LINK, OWNER)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(LINK+8, struct.pack('<I', capability))
    u.mem_write(LINK+0xc, bytes([0xaa, 0xbb]))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))
    install_cookie(u, pe)
    events = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xd4af88:
            assert reg(0) == PROVIDER
            events.append('handshake')
            ret(handshake_error)
        elif address == BASE+0xd39878:
            assert (reg(0), reg(1), reg(2)) == (OWNER, 2, 0)
            events.append('resource2_offset0')
            u.mem_write(reg(3), struct.pack('<I', stamp))
            ret(read_error)
        elif address == BASE+0xd4b568:
            assert (reg(0), reg(1), reg(2), reg(3), reg(7)) == (PROVIDER, 999, 0, 0, 0)
            events.append('query999')
            u.mem_write(reg(4), struct.pack('<H', firmware_error))
            u.mem_write(reg(5), struct.pack('<I', response))
            u.mem_write(reg(6), struct.pack('<I', response_size))
            ret(query_error)
        elif address == BASE+0xcd148:
            assert reg(1) == 0 and reg(2) == 4
            u.mem_write(reg(0), bytes(4))
            ret()
        elif address in (BASE+0xedef8, BASE+0xee038, BASE+0xd38a38,
                          BASE+0xd38758, BASE+0xd38810, BASE+0xce4b0):
            ret()

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, HFRP)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd3da30, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'version': list(u.mem_read(LINK+0xc, 2)), 'events': events}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []

    def check(stamp, expected, status=0, query=False, **kwargs):
        result = run(pe, stamp, **kwargs)
        assert result['status'] == status and result['version'] == expected, (stamp, kwargs, result)
        events = [] if kwargs.get('capability') == 0 else ['handshake']
        if events and not kwargs.get('handshake_error'):
            events += ['resource2_offset0'] + (['query999'] if query else [])
        assert result['events'] == events, result
        rows.append({'stamp': hex(stamp), 'inputs': kwargs, **result})

    for stamp in (0, 0x20250210):
        check(stamp, [0xaa, 0xbb], 0x56)
    for stamp in (0x20250211, 0x20250424):
        check(stamp, [1, 3])
    for stamp in (0x20250425, 0x2025fffe):
        check(stamp, [1, 5])
    for stamp in (0x2025ffff, 0x20260101, 0xffffffff):
        for response in (0x00010300, 0x000105ab, 0x99020733):
            check(stamp, [(response >> 16) & 255, (response >> 8) & 255], query=True, response=response)
    check(0x20260101, [0xaa, 0xbb], capability=0)
    check(0x20260101, [0xaa, 0xbb], 0x65, handshake_error=0x65)
    check(0x20260101, [0xaa, 0xbb], query=True, query_error=0x65)
    check(0x20260101, [1, 5], query=True, firmware_error=0x1234)
    check(0x20260101, [1, 5], query=True, response_size=0)
    check(0x20250425, [1, 5], read_error=0x65)
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'original_function': '0xd3da30', 'cases': rows,
                      'limitations': ['Handshake, resource read, query transport, locks, logging and memset are stubbed',
                                      'Actual HFRP stamp and version are not read from this machine',
                                      'Windows ignores some errors; this is evidence, not recommended Linux behavior']}, indent=2))


if __name__ == '__main__':
    main()
