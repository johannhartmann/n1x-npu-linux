#!/usr/bin/env python3
"""Execute HAL-1 RV32 command/ping/ACK code with RAM-only DMA and IRQs."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_profile1_boot import boot
from emulate_firmware_commands import ROOT, MMIO, HOST, STACK, STOP, ACK

CTX = 0x20010000


def run(blob, challenge=0x11111111, method=0x301, dma_error=None, acknowledge=True,
        host_handler=None, sequence=None, suppress_mask_set=False, host_submit=None, boot_from_reset=False, via_trap=False, trap_dma_ready=True, dma_register_model=False, host_payload=None, resume_scheduler=False, scheduler_handshake=True, scheduler_checkpoint=None, scheduler_registers=None):
    if resume_scheduler and not (via_trap and dma_register_model):
        raise ValueError('Scheduler tests require original reset, trap and DMA paths')
    if host_payload is not None and (not 4 <= len(host_payload) <= (STOP-HOST if boot_from_reset else 4096-(HOST & 4095))):
        raise ValueError('Host payload must fit the synthetic mapping without overlapping the stop page')
    host_bytes = 4 if host_payload is None else len(host_payload)
    if dma_register_model and (not boot_from_reset or dma_error is not None):
        raise ValueError('Register DMA model requires reset initialization and no high-level error injection')
    if via_trap and not boot_from_reset:
        raise ValueError('Trap tests require original reset initialization')
    if boot_from_reset:
        u, boot_state = boot(blob)
        assert boot_state['scheduler_wfi']
        for address, size in ((HOST & ~4095, ((HOST & 4095)+host_bytes+4095) & ~4095), (STOP, 0x1000)):
            u.mem_map(address, size)
        call_sp = boot_state['sp']
        stubs = () if dma_register_model else (0x104ed8, 0x105004)
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
    transfers, interrupts, register_writes, completed, method_reads = [], [], [], [], []
    trap_entries, trap_returns, dispatches, dma_commands = [], [], [], []
    scheduler_running = False
    scheduler_waiting = False
    checkpoint_reached = False
    task_events, task_interrupts = [], []
    scheduler_handshakes = 0
    scheduler_engine_reads = {}

    def reg(n):
        return u.reg_read(getattr(regs, f'UC_RISCV_REG_A{n}'))

    def hook(_, address, size, user):
        nonlocal scheduler_waiting, checkpoint_reached
        if scheduler_running and address in (0x104766, 0x1014f6, 0x103d56, 0x103f9c):
            task_events.append(hex(address))
        if scheduler_running and scheduler_checkpoint is not None and address == scheduler_checkpoint:
            checkpoint_reached = True
            u.emu_stop()
        if scheduler_running and address == 0x102194:
            scheduler_waiting = True
            u.emu_stop()
        if via_trap and address == 0x1002dc:
            trap_entries.append(address)
        if via_trap and address == 0x100324:
            trap_returns.append(address)
        if via_trap and address == 0x102232:
            dispatches.append((reg(0), reg(1)))
        if not boot_from_reset and address in (0x101c96, 0x1073ae, 0x1073d4, 0x100e14):
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif not dma_register_model and address in (0x104ed8, 0x105004):
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
        if offset not in (0, 4, 0x40, 0x44, 0x74, 0x10c, 0x110, 0x114, 0x118, 0x11c, 0x128, 0xd20, 0xd24):
            return
        assert size == 4
        register_writes.append((offset, value))
        if offset == 0x118 and dma_register_model:
            def word(register):
                return struct.unpack('<I', u.mem_read(MMIO+register, 4))[0]

            # Decode only the DMEM/channel-2 read/write command family observed here.
            assert value & ~0x720 == 0x2000
            length = 4 << ((value >> 8) & 7)
            external = (word(0x128) << 40) | (word(0x110) << 8)
            external += word(0x11c)
            local = 0x180000+word(0x114)
            assert HOST <= external and external+length <= HOST+host_bytes
            assert 0x180000 <= local and local+length <= 0x190000
            direction = 'write' if value & 0x20 else 'read'
            source, destination = (local, external) if direction == 'write' else (external, local)
            u.mem_write(destination, bytes(u.mem_read(source, length)))
            transfers.append(direction)
            dma_commands.append({'command': value, 'source': source, 'destination': destination,
                                 'bytes': length, 'direction': direction})
        elif offset in (0xd20, 0xd24):
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
            if value & mask & 64:
                task_interrupts.append({'pending': pending, 'mask': mask})
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

    def read(_, access, address, size, value, user):
        nonlocal scheduler_handshakes
        if scheduler_running and address-MMIO in (0x7008, 0x5000, 0x6000):
            key = hex(address-MMIO)
            scheduler_engine_reads[key] = scheduler_engine_reads.get(key, 0)+1
        if scheduler_running and address == MMIO+0x7c:
            previous = struct.unpack('<I', u.mem_read(address, 4))[0]
            if previous & 1:
                scheduler_handshakes += 1
                if scheduler_handshake:
                    u.mem_write(address, struct.pack('<I', previous & ~1))
        if dma_register_model and address == MMIO+0x118:
            # Synchronous fake DMA: not busy, idle bit set. No physical timing inferred.
            u.mem_write(address, struct.pack('<I', 2 if trap_dma_ready else 0))
        if via_trap and address in (MMIO+0x64, MMIO+0x68):
            method_reads.append((address-MMIO, struct.unpack('<I', u.mem_read(address, 4))[0]))

    u.hook_add(UC_HOOK_MEM_READ, read)
    u.hook_add(UC_HOOK_CODE, hook)
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    # Preserve firmware RAM and IRQ masks across commands. Enter either the
    # isolated dispatcher or the original trap under explicit synthetic IRQ state.
    for item in ([challenge] if sequence is None else sequence):
        u.mem_write(HOST, struct.pack('<I', item) if host_payload is None else bytes(host_payload))
        if host_submit is not None:
            host_submit(u)
            submitted_method, submitted_data = struct.unpack('<II', u.mem_read(MMIO+0x68, 8))
        else:
            submitted_method, submitted_data = method, HOST >> 8
        if via_trap:
            # Explicit synthetic one-entry method FIFO and external machine IRQ.
            # Host write-data is 0x6c; the original firmware reads receive-data 0x64.
            u.mem_write(MMIO+0x68, struct.pack('<I', submitted_method))
            u.mem_write(MMIO+0x64, struct.pack('<I', submitted_data))
            pending = struct.unpack('<I', u.mem_read(MMIO+8, 4))[0]
            u.mem_write(MMIO+8, struct.pack('<I', pending | 4))
            # Original ISR restores the DMA channel and waits for bit 1.
            u.mem_write(MMIO+0x118, struct.pack('<I', 2 if trap_dma_ready else 0))
            before = [u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32)]
            status = u.reg_read(regs.UC_RISCV_REG_MSTATUS)
            u.reg_write(regs.UC_RISCV_REG_MSTATUS,
                        (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
            u.reg_write(regs.UC_RISCV_REG_MCAUSE, 0x8000000b)
            u.reg_write(regs.UC_RISCV_REG_MEPC, STOP)
            entry = u.reg_read(regs.UC_RISCV_REG_MTVEC)
        else:
            u.reg_write(regs.UC_RISCV_REG_A0, submitted_method)
            u.reg_write(regs.UC_RISCV_REG_A1, submitted_data)
            u.reg_write(regs.UC_RISCV_REG_SP, call_sp)
            u.reg_write(regs.UC_RISCV_REG_RA, STOP)
            entry = 0x102232
        u.emu_start(entry, STOP, count=200000 if boot_from_reset else 2000)
        if via_trap and u.reg_read(regs.UC_RISCV_REG_PC) == STOP:
            after = [u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32)]
            assert before == after, 'Original ISR did not restore interrupted registers'
        if u.reg_read(regs.UC_RISCV_REG_PC) != STOP:
            break
        completed.append(struct.unpack('<I', u.mem_read(HOST, 4))[0])
    command_returned = u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    if resume_scheduler and command_returned:
        scheduler_running = True
        for offset, value in (scheduler_registers or {}).items():
            assert 0 <= offset <= 0x3fffc and offset % 4 == 0 and 0 <= value <= 0xffffffff
            u.mem_write(MMIO+offset, struct.pack('<I', value))
        # Trap restored the original WFI caller's registers and stack.
        # Resume at the original WFI return instruction; no task handler is called directly.
        u.emu_start(0x102198, STOP, count=200000)
    code_unmodified = bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
    if dma_register_model:
        assert code_unmodified
    return {'returned': command_returned,
            'pc': hex(u.reg_read(regs.UC_RISCV_REG_PC)),
            'host_word': struct.unpack('<I', u.mem_read(HOST, 4))[0],
            'host_payload_final': bytes(u.mem_read(HOST, host_bytes)).hex() if host_payload is not None else None,
            'transfers': transfers, 'interrupts': interrupts,
            'register_writes': register_writes, 'completed': completed,
            'boot_from_reset': boot_from_reset, 'boot_state': boot_state,
            'via_trap': via_trap, 'method_reads': method_reads,
            'trap_entries': trap_entries, 'trap_returns': trap_returns, 'dispatches': dispatches,
            'dma_commands': dma_commands, 'function_stubs': [hex(address) for address in stubs],
            'firmware_code_unmodified': code_unmodified,
            'first_task_slot': bytes(u.mem_read(0x180200, 256)).hex() if boot_from_reset else None,
            'scheduler_wfi': scheduler_waiting, 'task_events': task_events, 'task_interrupts': task_interrupts,
            'scheduler_handshakes': scheduler_handshakes,
            'scheduler_checkpoint_reached': checkpoint_reached,
            'scheduler_engine_reads': scheduler_engine_reads,
            'checkpoint_status': reg(0) if checkpoint_reached else None,
            'queue0': bytes(u.mem_read(0x180100, 32)).hex() if boot_from_reset else None,
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
