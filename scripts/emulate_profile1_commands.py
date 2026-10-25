#!/usr/bin/env python3
"""Execute HAL-1 RV32 command/ping/ACK code with RAM-only DMA and IRQs."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_profile1_boot import boot
from emulate_firmware_commands import ROOT, MMIO, HOST, STACK, STOP, ACK

CTX = 0x20010000


def run(blob, challenge=0x11111111, method=0x301, dma_error=None, acknowledge=True,
        host_handler=None, sequence=None, suppress_mask_set=False, host_submit=None, boot_from_reset=False):
    if boot_from_reset:
        u, boot_state = boot(blob)
        assert boot_state['scheduler_wfi']
        for address, size in ((HOST & ~4095, 0x1000), (STOP, 0x1000)):
            u.mem_map(address, size)
        call_sp = boot_state['sp']
        stubs = (0x104ed8, 0x105004)  # Only DMA remains an environment boundary.
    else:
        boot_state = None
        u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
        for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                              (MMIO, 0x2000), (CTX, 0x4000), (HOST & ~4095, 0x1000),
                              (STACK, 0x10000), (STOP, 0x1000)):
            u.mem_map(address, size)
        u.mem_write(0x100000, blob[512:512+36864])
        u.mem_write(0x180000, blob[512+36864:])
        u.mem_write(0x1862bc, struct.pack('<I', CTX))
        call_sp = STACK+0x8000
        stubs = (0x101c96, 0x1073ae, 0x1073d4, 0x100e14, 0x104ed8, 0x105004)
    for address in stubs:
        u.mem_write(address, bytes.fromhex('67800000'))
    u.mem_write(HOST, struct.pack('<I', challenge))
    u.mem_write(MMIO+0x40, struct.pack('<II', 0x11223344, 0x55667788))
    transfers, interrupts, register_writes, completed = [], [], [], []

    def reg(n):
        return u.reg_read(getattr(regs, f'UC_RISCV_REG_A{n}'))

    def hook(_, address, size, user):
        if not boot_from_reset and address in (0x101c96, 0x1073ae, 0x1073d4, 0x100e14):
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address in (0x104ed8, 0x105004):
            if address == 0x104ed8:
                source, destination, length = reg(0) | reg(1) << 32, reg(2), reg(3)
                direction = 'read'
            else:
                source, destination, length = reg(0), reg(1) | reg(2) << 32, reg(3)
                direction = 'write'
            assert length == 4
            assert source == HOST if direction == 'read' else destination == HOST
            transfers.append(direction)
            if direction != dma_error:
                u.mem_write(destination, bytes(u.mem_read(source, length)))
            u.reg_write(regs.UC_RISCV_REG_A0, 0xfffffffc if direction == dma_error else 0)

    def write(_, access, address, size, value, user):
        offset = address-MMIO
        if offset not in (0, 4, 0x40, 0x44, 0xd20, 0xd24):
            return
        assert size == 4
        register_writes.append((offset, value))
        if offset in (0xd20, 0xd24):
            mask = struct.unpack('<I', u.mem_read(MMIO+0xd28, 4))[0]
            if offset == 0xd20 and not suppress_mask_set:
                mask |= value
            elif offset == 0xd24:
                mask &= ~value
            u.mem_write(MMIO+0xd28, struct.pack('<I', mask))
        elif offset == 0:
            pending = struct.unpack('<I', u.mem_read(MMIO+8, 4))[0] | value
            u.mem_write(MMIO+8, struct.pack('<I', pending))
            mask = struct.unpack('<I', u.mem_read(MMIO+0xd28, 4))[0]
            if not (pending & mask & 128):
                return
            interrupts.append(struct.unpack('<II', u.mem_read(MMIO+0x40, 8)))
            if host_handler is not None:
                host_handler(u)
            elif acknowledge:
                u.mem_write(MMIO+0x40, struct.pack('<I', ACK))
        elif offset == 4:
            pending = struct.unpack('<I', u.mem_read(MMIO+8, 4))[0] & ~value
            u.mem_write(MMIO+8, struct.pack('<I', pending))

    u.hook_add(UC_HOOK_CODE, hook)
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    # Re-enter the real dispatcher without resetting firmware RAM or IRQ masks.
    # Only the synthetic host input and CPU entry registers change per command.
    for item in ([challenge] if sequence is None else sequence):
        u.mem_write(HOST, struct.pack('<I', item))
        if host_submit is not None:
            host_submit(u)
            submitted_method, submitted_data = struct.unpack('<II', u.mem_read(MMIO+0x68, 8))
        else:
            submitted_method, submitted_data = method, HOST >> 8
        u.reg_write(regs.UC_RISCV_REG_A0, submitted_method)
        u.reg_write(regs.UC_RISCV_REG_A1, submitted_data)
        u.reg_write(regs.UC_RISCV_REG_SP, call_sp)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(0x102232, STOP, count=200000 if boot_from_reset else 2000)
        if u.reg_read(regs.UC_RISCV_REG_PC) != STOP:
            break
        completed.append(struct.unpack('<I', u.mem_read(HOST, 4))[0])
    return {'returned': u.reg_read(regs.UC_RISCV_REG_PC) == STOP,
            'pc': hex(u.reg_read(regs.UC_RISCV_REG_PC)),
            'host_word': struct.unpack('<I', u.mem_read(HOST, 4))[0],
            'transfers': transfers, 'interrupts': interrupts,
            'register_writes': register_writes, 'completed': completed,
            'boot_from_reset': boot_from_reset, 'boot_state': boot_state,
            'final_irq_mask': struct.unpack('<I', u.mem_read(MMIO+0xd28, 4))[0],
            'final_irq_pending': struct.unpack('<I', u.mem_read(MMIO+8, 4))[0],
            'final_mailboxes': struct.unpack('<II', u.mem_read(MMIO+0x40, 8))}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for challenge in (0, 1, 0x11111111, 0x3fffffff):
        result = run(blob, challenge)
        assert result['returned'] and result['host_word'] == challenge*4
        assert result['transfers'] == ['read', 'write'] and result['interrupts'] == [(0x102, HOST >> 8)]
        assert result['final_mailboxes'] == (0x11223344, 0x55667788)
        rows.append({'case': f'ping_{challenge:x}', **result})
    result = run(blob, 0x40000000)
    assert result['returned'] and result['host_word'] == 0x40000000
    assert result['transfers'] == ['read'] and result['interrupts'] == [(0x110101, HOST >> 8)]
    rows.append({'case': 'ping_overflow', **result})
    for direction in ('read', 'write'):
        result = run(blob, dma_error=direction)
        assert result['returned'] and result['host_word'] == 0x11111111
        assert result['interrupts'] == [(0x40101, HOST >> 8)]
        rows.append({'case': 'dma_error_'+direction, **result})
    for command in (0, 2):
        result = run(blob, method=0x300 | command)
        assert result['returned'] and not result['transfers']
        assert result['interrupts'] == [(0x10001 | command << 8, HOST >> 8)]
        rows.append({'case': f'unsupported_command_{command}', **result})
    result = run(blob, method=1)
    assert result['returned'] and result['host_word'] == 0x44444444 and not result['interrupts']
    rows.append({'case': 'ping_without_response_flags', **result})
    result = run(blob, acknowledge=False)
    assert not result['returned'] and result['interrupts'] == [(0x102, HOST >> 8)]
    rows.append({'case': 'missing_host_ack_waits_at_observation_limit', **result})
    print(json.dumps({'passed': True, 'firmware_sha256': PROFILE1_HASH, 'architecture': 'RV32',
                      'hardware_access': False, 'dispatcher': '0x102232', 'ping': '0x10251c',
                      'response': '0x1021b2', 'cases': rows,
                      'limitations': ['Logging, command bookkeeping, hang query and DMA are stubbed',
                                      'Firmware context/global pointer is supplied by the emulator',
                                      'No physical IRQ/DMA or complete firmware boot was performed']}, indent=2))


if __name__ == '__main__':
    main()
