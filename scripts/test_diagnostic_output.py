#!/usr/bin/env python3
"""Execute original diagnostic result-check/copy IL with original HAL-1 DMA."""
import hashlib
import json
import struct
from unicorn import UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from inspect_loadable import Buffer
from diagnostic_fixture import DIAGNOSTIC_HASH


def execute(blob, instructions, observed, selector, success):
    live = []

    def response(u):
        live.append(u)
        for offset in (8, 0xd28):
            old = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
            u.mem_write(MMIO+offset, struct.pack('<I', old & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, method=0x301, host_payload=bytes(4096), boot_from_reset=True,
                 via_trap=True, dma_register_model=True, host_handler=response)
    assert result['returned'] and result['function_stubs'] == []
    u = live[0]
    context, task, cache, stop = 0x188000, 0x188100, 0x184400, 0x40000000
    u.mem_write(context, struct.pack('<I', task))
    u.mem_write(task, struct.pack('<QH', HOST+256, 9))
    u.mem_write(HOST+256, struct.pack('<9Q', *([0]*8+[HOST+1024])))
    u.mem_write(HOST+1024, b'\xa5'*32)

    def call(pc, a0, a1=0):
        u.reg_write(regs.UC_RISCV_REG_A0, a0)
        u.reg_write(regs.UC_RISCV_REG_A1, a1)
        u.reg_write(regs.UC_RISCV_REG_SP, 0x187000)
        u.reg_write(regs.UC_RISCV_REG_RA, stop)
        u.emu_start(pc, stop, count=100000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == stop
        value = u.reg_read(regs.UC_RISCV_REG_A0)
        return value-(1 << 32) if value >= 1 << 31 else value

    # Original cached-address and IL dispatch initialization. Kernel/task
    # state here is synthetic; address-list and output DMA are actual RV32 code.
    assert call(0x104a92, context) == 0
    assert call(0x102cec, 0) == 0
    u.mem_write(cache, instructions)
    saved = 0x2468ace0
    u.mem_write(MMIO+0x13004, struct.pack('<I', saved))
    u.mem_write(MMIO+0x13124, struct.pack('<I', observed))
    writes = []

    def record(_, access, address, size, value, data):
        if address-MMIO in (0x12004, 0x1703c, 0x18038):
            writes.append((address-MMIO, value))

    u.hook_add(UC_HOOK_MEM_WRITE, record)
    before = len(result['dma_commands'])
    status = call(0x102abe, context, 7)
    output = bytes(u.mem_read(HOST+1024, 32))
    transfers = result['dma_commands'][before:]
    if success:
        assert status == 0 and output == struct.pack('<I', observed)+b'\xa5'*28
        assert writes == [(0x12004, selector), (0x12004, saved), (0x1703c, 0), (0x18038, 1)]
        assert len(transfers) == 1
        assert transfers[0]['direction'] == 'write' and transfers[0]['bytes'] == 4
        assert transfers[0]['destination'] == HOST+1024
    else:
        assert status == -16 and output == b'\xa5'*32 and not transfers
        # Original batch stops at the assertion; restore/cleanup instructions
        # after it do not run. This is not a recovery-complete return.
        assert writes == [(0x12004, selector)]
        assert struct.unpack('<i', u.mem_read(context+48, 4))[0] == -16
    assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
    return {'selector_value': selector, 'observed_register': hex(observed),
            'firmware_return': status, 'output_written_bytes': 4 if success else 0,
            'trailing_output_unchanged_bytes': 28 if success else 32,
            'post_assert_restore_executed': success, 'register_writes': writes,
            'original_dma_commands': transfers, 'firmware_code_unmodified': True}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    assert hashlib.sha256(data).hexdigest() == DIAGNOSTIC_HASH
    view = Buffer(data)
    content = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        content[view.string(table, 0)] = data[extent.start:extent.stop]
    ib = content['task0-ib']
    first, second = ib[0x1e500:0x1e570], ib[0x1e700:0x1e770]
    assert len(first) == len(second) == 112
    assert struct.unpack('<BBHIII', first[32:48]) == (4, 1, 0, 0x13124, 0xffffffff, 0)
    assert struct.unpack('<BBHIII', second[32:48]) == (4, 4, 0, 0x13124, 0xffffffff, 0)
    assert first[48:64] == second[48:64] == bytes.fromhex('23000800000000002431010000000000')
    assert struct.unpack('<BBHIII', first[16:32]) == (32, 0, 0, 0x12004, 0, 0)
    assert struct.unpack('<BBHIII', second[16:32]) == (32, 0, 0, 0x12004, 1, 0)
    rows = []
    for value in (0, 1, 0xffffffff):
        rows.append(execute(blob, first, value, 0, value == 0))
    for value in (0, 1, 0x80000000, 0xffffffff):
        rows.append(execute(blob, second, value, 1, value > 0))
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'firmware_sha256': PROFILE1_HASH, 'diagnostic_sha256': DIAGNOSTIC_HASH,
                      'source_il_blocks': ['0x1e500..0x1e56f', '0x1e700..0x1e76f'],
                      'original_dispatcher': '0x102abe', 'original_assert_handler': '0x102a26',
                      'original_predicate': '0x102800', 'original_output_handler': '0x1029b0',
                      'cases': rows,
                      'limitations': ['Exact IL blocks called directly, not selected by a completed diagnostic scheduler',
                                      'Register results/selector values are modeled; comparator hardware and CRC meanings remain unknown',
                                      'Address-list context and MMIO/reset/DMA/IRQ effects are synthetic',
                                      'Only first 4 of 32 output bytes are written by these blocks; no full result schema inferred',
                                      'No firmware function stubs/code edits, physical diagnostic or numerical inference']}, indent=2))


if __name__ == '__main__':
    main()
