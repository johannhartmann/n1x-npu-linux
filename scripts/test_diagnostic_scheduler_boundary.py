#!/usr/bin/env python3
"""Expose the raw diagnostic fixture's zero-extension/IRQ progress boundary."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as regs
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from diagnostic_fixture import build, DIAGNOSTIC_HASH
from inspect_loadable import Buffer
from test_task_descriptor import Args


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    assert hashlib.sha256(data).hexdigest() == DIAGNOSTIC_HASH
    view = Buffer(data)
    contents = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        contents[view.string(table, 0)] = data[extent.start:extent.stop]
    assert len(contents['task0-addr0']) == 128
    records = []
    for index, offset, count in zip(range(5), (0x1e200, 0x1e400, 0x1e500, 0x1e600, 0x1e700), (18, 1, 7, 1, 7)):
        record = contents['task0-db'][256+index*64:320+index*64]
        assert len(record) == 64 and struct.unpack_from('<I', record)[0] == index
        assert struct.unpack_from('<IIHH', record, 4) == (1, offset, count, 16)
        records.append({'record_id': index, 'db_offset': 256+index*64,
                        'il_address_index': 1, 'il_offset': offset, 'instructions': count})
    with tempfile.TemporaryDirectory(prefix='n1x-diagnostic-boundary-') as tmp:
        library = Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_task.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    fenced = lib.n1x_dla_build_fenced_task_v2
    fenced.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Args), ctypes.c_uint64, ctypes.c_uint32]
    rows = []
    for irq in (0x400, 0x800):
        payload, addresses, layout = build(data, HOST)
        task = ctypes.create_string_buffer(256)
        args = Args(0, HOST+256, 1000000, 9)
        assert fenced(task, 256, ctypes.byref(args), HOST+768, 0x12345678) == 0
        payload[:256] = task.raw
        struct.pack_into('<9Q', payload, 256, *addresses)
        struct.pack_into('<I', payload, 768, 0xdeadbeef)
        live = []

        def response(u):
            live[:] = [u]
            for offset in (8, 0xd28):
                old = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
                u.mem_write(MMIO+offset, struct.pack('<I', old & ~128))
            u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

        result = run(blob, method=0x307, host_payload=payload, host_handler=response,
                     boot_from_reset=True, via_trap=True, dma_register_model=True,
                     resume_scheduler=True, scheduler_registers={0x7008: 1, 0x5000: 1, 0x6000: 1})
        assert result['wfi_return_address'] == 0x101424 and result['function_stubs'] == []
        assert result['interrupts'] == [(0x702, HOST >> 8)]
        u = live[0]

        def word(address):
            return struct.unpack('<I', u.mem_read(address, 4))[0]

        context = word(0x1862bc)+40
        internal, network = word(context), word(context+8)
        assert network == 0x185740
        assert bytes(u.mem_read(network, 128)) == contents['task0-addr0']
        assert bytes(u.mem_read(network+128, 128)) == bytes(128)
        assert [word(network+off) for off in (0xcc, 0xd0, 0xd4, 0xd8)] == [0]*4
        assert word(internal+40) == 5 and word(0x1862a4) == 1
        assert any(item['source'] == addresses[2]+256 and item['bytes'] == 64
                   for item in result['dma_commands'])
        entered = []

        def capture(_, pc, size, user):
            entered.append(pc)

        u.hook_add(UC_HOOK_CODE, capture, begin=0x1025e6, end=0x1025e6)
        before = len(result['trap_returns'])
        assert word(MMIO+0xd28) & irq
        u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | irq))
        status = u.reg_read(regs.UC_RISCV_REG_MSTATUS)
        u.reg_write(regs.UC_RISCV_REG_MSTATUS, (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
        u.reg_write(regs.UC_RISCV_REG_MCAUSE, 0x8000000b)
        u.reg_write(regs.UC_RISCV_REG_MEPC, 0x101424)
        u.emu_start(u.reg_read(regs.UC_RISCV_REG_MTVEC), 0x40000000, count=200000)
        assert entered == [0x1025e6] and len(result['trap_returns']) == before+1
        assert u.reg_read(regs.UC_RISCV_REG_PC) == 0x102194
        assert word(internal+40) == 5 and word(0x1862a4) == 1
        assert word(MMIO+8) & irq == 0
        assert word(HOST+768) == 0xdeadbeef and word(0x180114) == 1
        assert result['interrupts'] == [(0x702, HOST >> 8)] and not result['task_interrupts']
        assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
        rows.append({'modeled_irq_bit': hex(irq), 'original_engine_handler_entered': True,
                     'original_trap_returned': True, 'irq_cleared': True,
                     'remaining_records': 5, 'engine_state': 1, 'queue_active': True,
                     'fence_unchanged': True, 'firmware_code_unmodified': True})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'raw_network_initialized_bytes': 128, 'firmware_network_read_bytes': 256,
                      'network_irq_fields_from_synthetic_padding': ['0xcc', '0xd0', '0xd4', '0xd8'],
                      'db_records': records, 'cases': rows,
                      'limitations': ['Zero padding is synthetic fixture backing, not recovered Windows materialization',
                                      'Injected firmware IRQ bits do not identify physical P16 GSI routing',
                                      'No assertion that padding is the only missing condition or that a real Windows model is invalid',
                                      'Runtime materialization/version compatibility or opaque firmware differences remain hypotheses',
                                      'No guessed replacement fields, firmware code edits, physical diagnostic or numerical inference']}, indent=2))


if __name__ == '__main__':
    main()
