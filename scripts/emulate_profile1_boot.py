#!/usr/bin/env python3
"""Execute HAL-1 from its reset entry to scheduler WFI with explicit fake MMIO."""
import hashlib
import json
import struct
from collections import Counter
from unicorn import (Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ)
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT, MMIO

PHASES = (0x10032a, 0x106048, 0x105f5c, 0x1049ba, 0x100370, 0x10216c,
          0x100a64, 0x100e1e, 0x10724e, 0x104366, 0x1065ee, 0x105952, 0x104726)


def boot(blob, clear_handshake=True, busy_reads=0):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000), (MMIO, 0x40000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    phases, reads, writes = [], Counter(), []
    handshake_reads = 0
    handshake_pending = False
    waiting = False

    def code(_, address, size, user):
        nonlocal waiting
        if address in PHASES:
            phases.append(address)
        if address == 0x102194:  # Stop before WFI: no asynchronous IRQ delivery is modeled.
            assert bytes(u.mem_read(address, 4)) == bytes.fromhex('73005010')
            waiting = True
            u.emu_stop()

    def memory(_, access, address, size, value, user):
        nonlocal handshake_reads, handshake_pending
        if not MMIO <= address < MMIO+0x40000:
            return
        if access == UC_MEM_READ:
            reads[address-MMIO] += 1
            if address == MMIO+0x7c and handshake_pending:
                handshake_reads += 1
                if clear_handshake and handshake_reads > busy_reads:
                    previous = struct.unpack('<I', u.mem_read(address, 4))[0]
                    u.mem_write(address, struct.pack('<I', previous & ~1))
                    handshake_pending = False
        else:
            writes.append((address-MMIO, size, value))
            if address in (MMIO+0xd20, MMIO+0xd24):
                mask = struct.unpack('<I', u.mem_read(MMIO+0xd28, 4))[0]
                mask = mask | value if address == MMIO+0xd20 else mask & ~value
                u.mem_write(MMIO+0xd28, struct.pack('<I', mask))
            if address == MMIO+0x7c and value & 1:
                handshake_pending = True

    hooks = [u.hook_add(UC_HOOK_CODE, code),
             u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory)]
    u.emu_start(0x100000, 0x109000, count=200000)
    for hook in hooks:
        u.hook_del(hook)
    return u, {'scheduler_wfi': waiting, 'pc': u.reg_read(regs.UC_RISCV_REG_PC),
               'sp': u.reg_read(regs.UC_RISCV_REG_SP),
               'context': struct.unpack('<I', u.mem_read(0x1862bc, 4))[0],
               'phases': phases, 'writes': writes, 'reads': dict(reads),
               'handshake_poll_reads': handshake_reads,
               'mstatus': u.reg_read(regs.UC_RISCV_REG_MSTATUS),
               'mie': u.reg_read(regs.UC_RISCV_REG_MIE),
               'mtvec': u.reg_read(regs.UC_RISCV_REG_MTVEC)}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for busy in (0, 3, 17):
        _, result = boot(blob, busy_reads=busy)
        assert result['scheduler_wfi'] and result['pc'] == 0x102194
        assert result['context'] == 0x180100 and result['sp'] == 0x1872b0
        assert result['phases'] == list(PHASES)
        assert result['handshake_poll_reads'] == busy+1
        assert len(result['writes']) == 95
        assert (result['mstatus'], result['mie'], result['mtvec']) == (0x2008, 0x888, 0x1002dc)
        rows.append({'busy_reads': busy, 'reached_scheduler_wfi': True,
                     'context': hex(result['context']), 'writes': len(result['writes'])})
    _, stalled = boot(blob, clear_handshake=False)
    assert not stalled['scheduler_wfi'] and stalled['handshake_poll_reads'] > 1000
    assert stalled['pc'] in (0x100e4a, 0x100e4c, 0x100e4e)
    assert stalled['phases'] == list(PHASES[:8])
    print(json.dumps({'passed': True, 'hardware_access': False, 'entry': '0x100000',
                      'function_stubs': [], 'cases': rows,
                      'negative_control': 'Without modeled 0x7c bit-0 clearing, reset remains waiting',
                      'limitations': ['All MMIO and initial CPU CSR state are synthetic',
                                      'Clearing 0x7c bit 0 is an explicit model assumption, not a measured device event',
                                      'Stops before scheduler WFI; no asynchronous interrupt delivery',
                                      'No SROOT image authentication, physical boot, DMA or inference']}, indent=2))


if __name__ == '__main__':
    main()
