#!/usr/bin/env python3
"""Exercise original HAL-1 idle-delay consumer with synthetic timer MMIO.

Only outbound notification is intercepted; this is not a hardware timing test.
"""
import hashlib
import json
import struct
from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as regs
from emulate_profile1_boot import boot, ROOT, MMIO, PROFILE1_HASH


def execute(blob, flag, delay, elapsed, *, notified=0, activity=0, previous=0):
    u, state = boot(blob)
    assert state['scheduler_wfi']
    code = bytes(u.mem_read(0x100000, 0x9000))
    u.mem_write(0x186254, bytes((1, flag)) + struct.pack('<I', delay) + bytes(34))
    u.mem_write(0x18627d, bytes((notified,)))
    u.mem_write(0x186250, struct.pack('<I', activity))
    u.mem_write(0x185ba8, struct.pack('<Q', previous))
    u.mem_write(MMIO+0x2c, struct.pack('<Q', (previous+elapsed) % (1 << 64)))
    # Disable optional diagnostic DMA in original 0x10591a; no helper stub.
    u.mem_write(0x180100+1288, b'\0')
    events = []
    returned = False

    def hook(_, address, size, user):
        nonlocal returned
        if address == 0x102194:
            returned = True
            u.emu_stop()
        elif address == 0x10044c:
            events.append([u.reg_read(r) for r in
                           (regs.UC_RISCV_REG_A0, regs.UC_RISCV_REG_A1)])
            payload = u.reg_read(regs.UC_RISCV_REG_A2)
            assert bytes(u.mem_read(payload, 12)) == bytes(12)
            u.reg_write(regs.UC_RISCV_REG_PC, u.reg_read(regs.UC_RISCV_REG_RA))

    u.reg_write(regs.UC_RISCV_REG_RA, 0x102194)
    handle = u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x107318, 0x109000, count=10000)
    u.hook_del(handle)
    assert returned and bytes(u.mem_read(0x100000, 0x9000)) == code
    return events, u.mem_read(0x18627d, 1)[0]


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for flag in (0, 1, 2, 255):
        for delay in (0, 1, 19200, 0xffffffff):
            threshold = delay*1000
            for elapsed in sorted({max(0, threshold-1), threshold, threshold+1}):
                for notified, activity in ((0, 0), (1, 0), (0, 1)):
                    for previous in (0, (1 << 32)-10, (1 << 64)-10):
                        events, after = execute(blob, flag, delay, elapsed,
                                                notified=notified, activity=activity,
                                                previous=previous)
                        expected = flag != 2 and not notified and not activity and elapsed > threshold
                        assert events == ([[12, 0]] if expected else [])
                        assert after == (1 if expected else notified)
                        rows.append(expected)
    print(json.dumps({'passed': True, 'cases': len(rows),
                      'notifications': sum(rows), 'hardware_access': False,
                      'consumer': '0x107318', 'firmware_code_unmodified': True,
                      'function_stubs': ['0x10044c outbound notification boundary'],
                      'observations': ['flag 2 suppresses notification; 0, 1 and 255 do not',
                                       'notification requires elapsed > uint32_delay * 1000',
                                       'already-notified byte or active word suppresses notification',
                                       'elapsed subtraction wraps as unsigned 64-bit'],
                      'limitations': ['Timer ticks are synthetic; physical tick duration and provider units remain unknown',
                                      'Notification transport is intercepted; no physical firmware boot or inference']}, indent=2))


if __name__ == '__main__':
    main()
