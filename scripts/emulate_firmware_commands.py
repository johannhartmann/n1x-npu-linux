#!/usr/bin/env python3
"""Run the original RISC-V ping/dispatch/response code with RAM DMA and IRQs.

Logging and the engine-hang query are stubbed. DMA is a Python RAM copy; the
host ACK is injected into a RAM register page. No hardware backend is opened.
"""
import hashlib
import json
import struct
from pathlib import Path
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from emulate_task_header_checks import BLOB_HASH

ROOT = Path(__file__).resolve().parents[1]
MMIO = 0x1400000
CMD, HOST, STACK, STOP = 0x20000000, 0x20001000, 0x20100000, 0x20200000
ACK = 0xd1a0cafe


def run(blob, challenge=0x11111111, method=0x301, dma_error=None, acknowledge=True, host_handler=None, payload=None, memory=None, observe=()):
    uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for addr, size in ((0x100000, 0xb000), (0x180000, 0x10000), (MMIO, 0x2000),
                       (CMD, 0x2000), (STACK, 0x10000), (STOP, 0x1000)):
        uc.mem_map(addr, size)
    uc.mem_write(0x100000, blob[0x200:0xb100])
    uc.mem_write(0x180000, blob[0xb100:])
    ret = bytes.fromhex('67800000')
    for address in (0x101e9a, 0x101686, 0x10559e, 0x105662):
        uc.mem_write(address, ret)
    uc.mem_write(CMD, struct.pack('<III', 0, method, HOST >> 8))
    uc.mem_write(HOST, struct.pack('<I', challenge) if payload is None else bytes(payload))
    for address, data in (memory or {}).items():
        uc.mem_write(address, bytes(data))
    previous = (0x11223344, 0x55667788)
    uc.mem_write(MMIO+0x40, struct.pack('<II', *previous))
    dma, interrupts, transfers = [], [], []

    def hook(_, address, size, user):
        if address in (0x101e9a, 0x101686):
            uc.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address in (0x10559e, 0x105662):
            source = uc.reg_read(regs.UC_RISCV_REG_A0)
            destination = uc.reg_read(regs.UC_RISCV_REG_A1)
            length = uc.reg_read(regs.UC_RISCV_REG_A2)
            direction = 'host_to_firmware' if address == 0x10559e else 'firmware_to_host'
            if payload is None:
                assert length == 4
            host_address = source if direction == 'host_to_firmware' else destination
            assert HOST <= host_address and host_address+length <= CMD+0x2000
            if payload is None:
                assert host_address == HOST
            dma.append(direction)
            transfers.append({'direction': direction, 'source': hex(source),
                              'destination': hex(destination), 'length': length})
            if direction == dma_error:
                uc.reg_write(regs.UC_RISCV_REG_A0, (1 << 64)-4)
            else:
                uc.mem_write(destination, bytes(uc.mem_read(source, length)))
                uc.reg_write(regs.UC_RISCV_REG_A0, 0)

    def write(_, access, address, size, value, user):
        if address == MMIO and value == 0x80:
            interrupts.append(struct.unpack('<II', uc.mem_read(MMIO+0x40, 8)))
            if host_handler is not None:
                host_handler(uc)
            elif acknowledge:
                uc.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    uc.hook_add(UC_HOOK_CODE, hook)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.reg_write(regs.UC_RISCV_REG_A0, CMD)
    uc.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    uc.reg_write(regs.UC_RISCV_REG_RA, STOP)
    uc.emu_start(0x102382, STOP, count=1000)
    return {'returned': uc.reg_read(regs.UC_RISCV_REG_PC) == STOP,
            'pc': hex(uc.reg_read(regs.UC_RISCV_REG_PC)),
            'host_word': struct.unpack('<I', uc.mem_read(HOST, 4))[0],
            'dma': dma, 'interrupts': interrupts, 'transfers': transfers,
            'observed': {hex(address): bytes(uc.mem_read(address, size)).hex() for address, size in observe},
            'final_mailboxes': struct.unpack('<II', uc.mem_read(MMIO+0x40, 8))}


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    rows = []
    for challenge in (0, 1, 0x11111111, 0x3fffffff):
        result = run(blob, challenge)
        assert result['returned'] and result['host_word'] == challenge*4
        assert result['dma'] == ['host_to_firmware', 'firmware_to_host']
        assert result['interrupts'] == [(0x102, HOST >> 8)]
        assert result['final_mailboxes'] == (0x11223344, 0x55667788)
        rows.append({'case': 'ping_'+hex(challenge), **result})
    result = run(blob, 0x40000000)
    assert result['returned'] and result['host_word'] == 0x40000000
    assert result['interrupts'] == [(0x110101, HOST >> 8)]
    assert result['dma'] == ['host_to_firmware']
    rows.append({'case': 'ping_multiplication_overflow', **result})
    for direction in ('host_to_firmware', 'firmware_to_host'):
        result = run(blob, dma_error=direction)
        assert result['returned'] and result['host_word'] == 0x11111111
        assert result['interrupts'] == [(0x40101, HOST >> 8)]
        rows.append({'case': 'dma_failure_'+direction, **result})
    for command in (0, 2, 26):
        result = run(blob, method=0x300 | command)
        assert result['returned'] and not result['dma']
        assert result['interrupts'] == [(0x10001 | (command << 8), HOST >> 8)]
        rows.append({'case': 'unsupported_command_'+str(command), **result})
    result = run(blob, method=1)
    assert result['returned'] and result['host_word'] == 0x44444444 and not result['interrupts']
    rows.append({'case': 'ping_without_response_flags', **result})
    result = run(blob, acknowledge=False)
    assert not result['returned'] and result['interrupts'] == [(0x102, HOST >> 8)]
    assert result['pc'] in ('0x102354', '0x102362', '0x102364', '0x102366')
    rows.append({'case': 'missing_host_ack_still_waiting_at_instruction_budget', **result})
    table = 0x10ad88
    dispatch = [{'command': i+1, 'target': hex(table+struct.unpack_from('<i', blob, 0x200+table-0x100000+i*4)[0])}
                for i in range(25)]
    print(json.dumps({'scope': 'Original RISC-V dispatcher, ping and mailbox handshake with RAM-only backends',
                      'firmware_sha256': BLOB_HASH, 'dispatcher': '0x102382',
                      'ping_handler': '0x102576', 'response_helper': '0x102326',
                      'host_ack': hex(ACK), 'command_dispatch_table': dispatch, 'cases': rows,
                      'limitations': ['DMA engines, interrupt routing and hardware execution are not emulated',
                                      'Engine-hang query and logging are stubbed',
                                      'No task submission or inference was executed'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
