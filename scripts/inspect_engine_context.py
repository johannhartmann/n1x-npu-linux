#!/usr/bin/env python3
"""Trace compiler context IL through unmodified firmware; no hardware access."""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as rv

from test_profile1_context_save import check
from test_profile1_active_preemption import start_task
from inspect_runtime_command import image, RUNTIME_HASH, BASE as RBASE
from inspect_kmd_task_submission import ROOT, KMD_SHA256
from emulate_profile1_commands import run, HOST, MMIO, ACK, PROFILE1_HASH
from inspect_loadable import Buffer, inspect
from verify_relu_loadable import LOADABLE_HASH


def check_address_instruction(firmware):
    """Exercise opcode 0x21 directly, including RV32 carry and special index."""
    live = []

    def ack(u):
        live.append(u)
        for offset in (8, 0xd28):
            value = int.from_bytes(u.mem_read(MMIO+offset, 4), 'little')
            u.mem_write(MMIO+offset, struct.pack('<I', value & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(firmware, method=0x301, host_payload=bytes(4096), boot_from_reset=True,
                 via_trap=True, dma_register_model=True, host_handler=ack)
    assert result['returned'] and result['function_stubs'] == []
    u = live[0]
    context, task, instruction, stop = 0x188000, 0x188100, 0x188200, 0x40000000
    u.mem_write(context, struct.pack('<I', task))
    u.mem_write(task, struct.pack('<QH', HOST+256, 2))
    u.mem_write(HOST+256, struct.pack('<QQ', 0xfffffff0, 0xffffffffffffff00))

    def call(pc):
        for register, value in ((rv.UC_RISCV_REG_A0, context),
                                (rv.UC_RISCV_REG_A1, instruction),
                                (rv.UC_RISCV_REG_SP, 0x187000),
                                (rv.UC_RISCV_REG_RA, stop)):
            u.reg_write(register, value)
        u.emu_start(pc, stop, count=100000)
        assert u.reg_read(rv.UC_RISCV_REG_PC) == stop
        value = u.reg_read(rv.UC_RISCV_REG_A0)
        return value if value < 1 << 31 else value-(1 << 32)

    assert call(0x104a92) == 0
    cases = []
    for index, offset, expected in ((0, 0, 0xfffffff0), (0, 0x40, 0x100000030),
                                    (1, 0x200, 0x100), (2, 0, 0), (3, 0, None),
                                    (0xffff, 0, 0xffffffff), (0xffff, 1, 0x100000000),
                                    (0xffff, 0x100, 0x1000000ff)):
        u.mem_write(instruction, struct.pack('<BBHIII', 0x21, 0, index,
                                             0x25018, 0x2501c, offset))
        u.mem_write(MMIO+0x25018, b'\xa5'*8)
        status = call(0x102962)
        output = bytes(u.mem_read(MMIO+0x25018, 8))
        assert status == (-3 if expected is None else 0)
        assert output == (b'\xa5'*8 if expected is None else struct.pack('<Q', expected))
        cases.append({'index': index, 'offset': offset, 'status': status,
                      'register_pair': output.hex()})
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    return cases


def trace(runtime, pe, firmware, model, irq=0x400):
    records = []
    layout = None

    def start(*args, **kwargs):
        nonlocal layout
        fixture = start_task(*args, **kwargs)
        layout = fixture['layout']
        u = fixture['u']
        active = None

        def code(_, pc, size, user):
            nonlocal active
            if pc in (0x105a08, 0x105a6a):
                active = {'phase': 'save' if pc == 0x105a08 else 'restore',
                          'slices': [], 'instructions': [], 'register_writes': []}
                records.append(active)
            elif pc in (0x105a42, 0x105aa4):
                assert active is not None
                active['status'] = u.reg_read(rv.UC_RISCV_REG_A0)
                active = None
            elif active is not None and pc == 0x102d7a:
                address = u.reg_read(rv.UC_RISCV_REG_A1)
                active['slices'].append(bytes(u.mem_read(address, 16)).hex())
            elif active is not None and pc == 0x102b5c:
                address = u.reg_read(rv.UC_RISCV_REG_A1)
                active['instructions'].append({
                    'bytes': bytes(u.mem_read(address, 16)).hex(),
                    'handler': hex(u.reg_read(rv.UC_RISCV_REG_A5))})

        def write(_, access, address, size, value, user):
            if active is not None and MMIO+0x3000 <= address < MMIO+0x30000:
                active['register_writes'].append([hex(address-MMIO), size, hex(value)])

        u.hook_add(UC_HOOK_CODE, code)
        u.hook_add(UC_HOOK_MEM_WRITE, write)
        return fixture

    result = check(runtime, pe, firmware, model, flags=128, irq=irq, start=start)
    assert result['firmware_code_unmodified'] and result['output_still_poisoned']
    view = Buffer(model)
    blobs = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        blobs[view.string(table, 0)] = model[extent.start:extent.stop]
    metadata = inspect(model)
    addresses = [layout[e['memory_id']]['address']+e['offset']
                 for e in metadata['addresses']]
    addresses = [addresses[i] for i in metadata['tasks'][0]['address_ids']]
    assert len(records) == 4
    # The compiler schedules save records in one order and restore in reverse.
    expected = [('save', 0x6a00, 25, 0xe00, 0),
                ('save', 0x6c00, 9, 0x1500, 0x29cb00),
                ('restore', 0x6e00, 25, 0x2c00, 0x29cb00),
                ('restore', 0x7000, 15, 0x4300, 0)]
    for record, (phase, offset, count, program, context_offset) in zip(records, expected):
        assert record['phase'] == phase and record['status'] == 0
        assert record['slices'] == [struct.pack('<IIHHI', 3, offset, count, 16, 0).hex()]
        instructions = [bytes.fromhex(i['bytes']) for i in record['instructions']]
        assert len(instructions) == count
        assert b''.join(instructions) == blobs['task-0-inst'][offset:offset+count*16]
        writes, relocations = [], []
        for ins, observed in zip(instructions, record['instructions']):
            opcode, _, index, low, high, addend = struct.unpack('<BBHIII', ins)
            if opcode == 1:
                assert observed['handler'] == '0x102888'
                assert ins == bytes.fromhex('01040000445002000f00000000000000')
            elif opcode == 0x20:
                assert observed['handler'] == '0x1027a8'
                writes.append([hex(low), 4, hex(high)])
            else:
                assert opcode == 0x21 and observed['handler'] == '0x102962'
                value = addresses[index]+addend
                writes.extend([[hex(low), 4, hex(value & 0xffffffff)],
                               [hex(high), 4, hex(value >> 32)]])
                relocations.append([index, addend, low, high])
        assert record['register_writes'] == writes
        context_register = 0x25018 if phase == 'save' else 0x25010
        assert relocations == [[3, program, 0x25000, 0x25004],
                               [5, context_offset, context_register, context_register+4]]
        # Do not infer a transfer byte length from these engine control words.
        record['program_offset'] = program
        record['context_offset'] = context_offset
        record['context_offset_equals_logical_size'] = context_offset == layout[6]['bytes']
        record['programmed_context_address'] = hex(addresses[5]+context_offset)
        record['instruction_offset'] = offset
    assert layout[6]['bytes'] == 0x29cb00
    return records


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, RBASE)
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(model).hexdigest() == LOADABLE_HASH
    records = trace(runtime, pe, firmware, model)
    assert trace(runtime, pe, firmware, model, irq=0x800) == records
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'engine_irqs': ['0x400', '0x800'], 'records': records,
                      'address_instruction_cases': check_address_instruction(firmware),
                      'firmware_sha256': PROFILE1_HASH,
                      'kmd_sha256': KMD_SHA256, 'runtime_sha256': RUNTIME_HASH,
                      'loadable_sha256': LOADABLE_HASH,
                      'limitations': ['Synthetic MMIO, IRQs and RAM DMA',
                                      'Engine transfers and arithmetic are not modeled',
                                      'End-of-allocation pointer is observed, not proof of an out-of-bounds transfer',
                                      'No physical preemption, context preservation or DMA-quiescence proof']}, indent=2))


if __name__ == '__main__':
    main()
