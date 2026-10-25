#!/usr/bin/env python3
"""Execute complete original method-2 start orchestrator with synthetic HAL I/O."""
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
CTX, QUEUE, VTABLE, OUTER, STACK, STUB = (0x26000000+i*0x10000 for i in range(6))
STOP = STUB+0xff0


def run(pe, force_upload=0, request_error=0, busy=0, idle_error=0, final_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd47000, 0x1000), (0xd4c000, 0x1000), (0xd57000, 0x1000),
                       (0xd39000, 0x1000), (0xd38000, 0x1000), (0xd3b000, 0x1000),
                       (0x238000, 0x1000), (0xe3e000, 0x1000), (0x1512000, 0x1000),
                       (0x13ac000, 0x1000)):
        u.mem_map(BASE+rva, size)
        if rva in (0xd47000, 0xd4c000):
            u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, QUEUE, VTABLE, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160, 0xd38758, 0xd3bfd8, 0x2385c0):
        u.mem_write(BASE+rva, ret)
    for offset in (0, 0x10, 0x20, 0x30, 0x40):
        u.mem_write(STUB+offset, ret)
    # Original wait helper's delay and error logger are OS/environment boundaries.
    u.mem_map(BASE+0xd8000, 0x1000)
    u.mem_write(BASE+0xd82c0, ret)
    u.mem_map(BASE+0xce000, 0x1000)
    u.mem_write(BASE+0xce4b0, ret)
    for address, value in ((BASE+0xe3eaf0, STUB), (CTX+0x400, STUB+0x10),
                           (CTX+0x408, STUB+0x20), (CTX+0xd8, STUB+0x30),
                           (CTX+0x68, QUEUE), (QUEUE, VTABLE), (VTABLE+0x1d0, STUB+0x40)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(CTX+0x460, struct.pack('<I', 2))
    u.mem_write(CTX+0x486, bytes([force_upload]))
    u.mem_write(QUEUE+0x50, struct.pack('<I', 0xa5))
    events, delays, polls = [], [], []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address in (STUB+0x10, STUB+0x20):
            assert reg(0) == OUTER and reg(1) == CTX
            if address == STUB+0x10:
                assert [reg(i) for i in (2, 3, 4)] == [0, 0, 1]
            events.append('upload' if address == STUB+0x20 else 'reinit')
            u.reg_write(regs.UC_ARM64_REG_X0, request_error)
        elif address == STUB+0x40:
            assert reg(0) == OUTER and reg(1) == QUEUE
            value = int(len(polls) < busy)
            polls.append(value)
            events.append('idle_read')
            u.mem_write(reg(2), struct.pack('<I', value))
            u.reg_write(regs.UC_ARM64_REG_X0, idle_error)
        elif address == STUB+0x30:
            assert [reg(i) for i in (0, 1, 2)] == [OUTER, CTX, 0xa5]
            events.append('final_hal_d8')
            u.reg_write(regs.UC_ARM64_REG_X0, final_error)
        elif address == BASE+0xd82c0:
            delays.append(reg(0))
        elif address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        elif address in (BASE+0x2385c0, BASE+0xd38758, BASE+0xd3bfd8):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd47430, STOP, count=1000000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'events': events, 'polls': polls, 'delays': delays}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for force_upload in (0, 1):
        for kwargs, status, reads, final in (({}, 0, 1, True), ({'busy': 3}, 0, 4, True),
                                            ({'request_error': 0xffff}, 0xffff, 0, False),
                                            ({'idle_error': 0x65}, 0x65, 1, False),
                                            ({'busy': 9999}, 0x65, 5001, False),
                                            ({'final_error': 0xffff}, 0, 1, True)):
            result = run(pe, force_upload=force_upload, **kwargs)
            assert result['status'] == status
            assert result['events'] == ['upload' if force_upload else 'reinit']+['idle_read']*reads+(['final_hal_d8'] if final else [])
            assert all(value == 100 for value in result['delays'])
            rows.append({'force_upload': force_upload, 'injected': kwargs, 'status': status,
                         'idle_reads': reads, 'final_hal_callback': final, 'delay_calls': len(result['delays'])})
    print(json.dumps({'passed': True, 'hardware_access': False, 'entry': '0xd47430', 'cases': rows,
                      'limitations': ['Original orchestration and wait execute; HAL requests/accessors are stubs',
                                      'Optional HAL feature flags are zero, other branches remain untested',
                                      'No physical boot, protocol readiness or DMA quiescence established']}, indent=2))


if __name__ == '__main__':
    main()
