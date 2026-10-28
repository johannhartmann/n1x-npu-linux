#!/usr/bin/env python3
"""Execute the original Windows KMD v3 descriptor builder in RAM only."""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as rv

from inspect_kmd_task_submission import (Machine, BASE, OBJECT, PARENT, PRIMARY,
                                         OWNER, DESCRIPTOR, JOB, STUB, ROOT,
                                         KMD_SHA256, HOST)
from inspect_runtime_command import Builder, image, RUNTIME_HASH, BASE as RBASE, MEM, CPU, OUT
from diagnostic_fixture import build_relu
from emulate_profile1_commands import run, MMIO, ACK, PROFILE1_HASH


def build(pe, *, sequence=1, command=0x12345678000, length=64,
          metadata=True, job_flags=0, disable_pre=False, context=False,
          context_address=0x3456789a000):
    m = Machine(pe)
    for page in (0xe9c000, 0xcd000, 0x14c1000):
        m.u.mem_map(BASE+page, 0x1000)
        m.u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    # Use the real class table, including descriptor size, context-header
    # size/address and context-header initializer, not invented virtual slots.
    assert m.read(BASE+0xe9cbc8, 'Q') == (BASE+0xdd7460,)
    assert m.read(BASE+0xe9cbf0, 'Q') == (BASE+0xdd73f0,)
    m.write(OBJECT, 'Q', BASE+0xe9cb68)
    stride = m.call(0xdd73b0, OBJECT)
    assert stride == 0x500
    gpu_ring = HOST-(sequence & 31)*stride
    for address, pointer in ((OBJECT+0x20, PARENT), (PARENT+0x6ec0, PRIMARY),
                             (PRIMARY+0xed8, OWNER), (OWNER+0x18, OWNER+0x1000),
                             (OWNER+0x1000+0x4e0, OWNER+0x2000),
                             (PRIMARY, OWNER+0x4000),
                             (OWNER+0x4000+0x268, STUB+4),
                             (OBJECT+0x28, gpu_ring), (OBJECT+0x30, DESCRIPTOR),
                             (JOB+0xf8, OWNER+0x5000 if metadata else 0)):
        m.write(address, 'Q', pointer)
    m.write(OBJECT+0x18, 'I', stride)
    m.write(OWNER+0x5000+0x38, 'Q', command)
    m.write(OWNER+0x3000+4, 'I', 4 if disable_pre else 0)
    m.write(OWNER+0x2000+0xf00, 'I', 4)
    m.write(OWNER+0x2000+0xefc, 'I', 8)
    m.write(OWNER+0x1000+0x4e8, 'Q', HOST+0x6000)
    for offset, index in ((0x508, 0x10), (0x52c, 0x100), (0x520, 0x200)):
        m.write(OWNER+0x1000+offset, 'III', index, 256, 4)
    m.write(JOB+0x20, 'I', 2)
    m.write(JOB+0x2c, 'I', sequence)
    m.write(JOB+0x34, 'I', length)
    m.write(JOB+0x1e8, 'I', job_flags)
    if context:
        m.write(JOB+0x10, 'Q', OWNER+0x6000)
        m.write(OWNER+0x6000+0x70, 'I', 2)
        m.write(OWNER+0x6000+0x2b0, 'B', 7)
        m.write(OWNER+0x6000+0x2c8, 'Q', context_address)
    visited, events = set(), []

    def hook(_, pc, size, user):
        visited.add(pc-BASE)
        if pc == STUB+4:
            assert [m.reg(i) for i in (0, 1)] == [PRIMARY, 2]
            m.ret(OWNER+0x3000)
        elif pc == BASE+0xcd148:
            assert m.reg(1) == 0 and m.reg(2) == 64
            m.u.mem_write(m.reg(0), bytes(64))
            events.append('zero_context_header')
            m.ret(m.reg(0))
        elif pc == BASE+0xce478:
            events.append('malformed_command_length_log')
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    assert m.call(0xdd7460, OBJECT, JOB) == 0
    offset = (sequence & 31)*stride
    descriptor = bytes(m.u.mem_read(DESCRIPTOR+offset, stride))
    context_offset = 32*stride+(sequence & 31)*64
    context_header = bytes(m.u.mem_read(DESCRIPTOR+context_offset, 64))
    context_gpu = gpu_ring+context_offset
    effective_length = length if metadata else 0
    valid = effective_length != 0 and effective_length % 64 == 0
    expected = bytearray(stride)
    struct.pack_into('<QBBHHBBHHB', expected, 0, 0, 3, 0x44, stride,
                     sequence & 0xffff, int(not disable_pre), 1, 60, 64, 0)
    struct.pack_into('<QH', expected, 21, command if valid else 0,
                     (effective_length >> 6) & 0xffff if valid else 0)
    struct.pack_into('<QHQQ', expected, 33, 1800,
                     4 | (2 if job_flags & 128 else 0) | int(not valid),
                     0, context_gpu)
    done, started, control = HOST+0x6060, HOST+0x6440, HOST+0x6840
    pre = (b'\x92'+struct.pack('<QI', done, (sequence-1)&0xffffffff)+
           b'\x80'+struct.pack('<QI', started, sequence)+
           b'\x90'+struct.pack('<QI', control, 0xfeed)+b'\0')
    post = b'\x80'+struct.pack('<QI', done, sequence)+b'\0'
    if not disable_pre:
        struct.pack_into('<HH', expected, 60, 68, len(pre))
        expected[68:68+len(pre)] = pre
    struct.pack_into('<HH', expected, 64, 0x1e5, len(post))
    expected[0x1e5:0x1e5+len(post)] = post
    assert descriptor == expected
    expected_context = bytearray(64)
    expected_context[0] = 1
    if context:
        expected_context[1] = 7
        struct.pack_into('<Q', expected_context, 4, context_address)
    assert context_header == expected_context
    assert {0xdd6638, 0xdd6540, 0xdd64d8, 0xdd6ab0, 0xdd6a08, 0xdd73f0} <= visited
    assert events == (['malformed_command_length_log'] if effective_length and not valid else [])+['zero_context_header']
    return descriptor, context_gpu, context_header, {
        'sequence': sequence, 'command_address': hex(command), 'command_bytes': length,
        'metadata': metadata, 'preactions': not disable_pre, 'job_flags': job_flags,
        'firmware_flags': struct.unpack_from('<H', descriptor, 41)[0],
        'umd_count': struct.unpack_from('<H', descriptor, 29)[0],
        'context_address': hex(context_gpu), 'events': events}


def firmware_chain(runtime, pe, firmware, model, engine_status, umd_flags):
    """Original UMD -> original KMD packet -> unmodified RV32 scheduler/ISR.

    Allocation/mapping, DMA transfers, MMIO and engine completion are modeled.
    Model arithmetic is deliberately absent; its output must stay poisoned.
    """
    backing, addresses, layout = build_relu(model, HOST+0x10000)
    payload = bytearray(0x10000)+backing
    b = Builder(runtime)
    b.gpu[MEM] = HOST+0x1000
    b.addresses([(0, address) for address in addresses])
    b.fences([(4, 1, HOST+0x7000, 0x11223344)])
    assert b.build(flags=umd_flags, field=0) == 0
    payload[0x1000:0x5000] = b.u.mem_read(CPU, 0x4000)
    descriptor, ctx, ctxdata, report = build(pe, command=b.get(OUT))
    payload[:len(descriptor)] = descriptor
    payload[ctx-HOST:ctx-HOST+len(ctxdata)] = ctxdata
    for offset, value in ((0x6060, 0), (0x6440, 0xdeadbeef),
                          (0x6840, 0xfeed), (0x7000, 0xdeadbeef)):
        struct.pack_into('<I', payload, offset, value)
    input_data = (ROOT/'results/relu-probe/input-chw16-fp16.bin').read_bytes()
    assert hashlib.sha256(input_data).hexdigest() == '73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45'
    for memory, values in ((1, input_data), (2, b'\xa5'*3136)):
        start = 0x10000+layout[memory]['offset']
        payload[start:start+3136] = values
    live, consumer = [], []

    def response(u):
        live[:] = [u]
        def command_branch(_, pc, size, user):
            if pc in (0x101938, 0x101a9c, 0x101a9e):
                consumer.append(hex(pc))
        u.hook_add(UC_HOOK_CODE, command_branch, begin=0x101938, end=0x101a9e)
        for offset in (8, 0xd28):
            value = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
            u.mem_write(MMIO+offset, struct.pack('<I', value & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(firmware, method=0x307, host_payload=payload, host_handler=response,
                 boot_from_reset=True, via_trap=True, dma_register_model=True,
                 resume_scheduler=True)
    assert result['returned'] and result['scheduler_wfi']
    assert result['function_stubs'] == [] and result['firmware_code_unmodified']
    assert result['interrupts'] == [(0x702, HOST >> 8)]
    u = live[0]

    def word(address):
        return struct.unpack('<I', u.mem_read(address, 4))[0]

    assert word(HOST+0x7000) == 0x11223344
    assert '0x101938' in consumer
    if umd_flags & 255:
        assert engine_status is None
        assert result['wfi_return_address'] == 0x10497a
        assert '0x101a9e' in consumer and '0x101a9c' not in consumer
        assert word(HOST+0x6060) == word(HOST+0x6440) == 1
        assert word(0x180114) == word(0x1862a4) == 0
        assert len(result['task_interrupts']) == 1
        assert not any(0x2505c <= address <= 0x25090
                       for address, _, _ in result['scheduler_mmio_writes'])
        assert bytes(u.mem_read(layout[1]['address'], 3136)) == input_data
        assert bytes(u.mem_read(layout[2]['address'], 3136)) == b'\xa5'*3136
        return {'case': 'umd_execution_bypass', 'umd_flags': umd_flags,
                'consumer_branches': consumer, 'umd_prefence_written': True,
                'outer_completion_written': True, 'task_irq_asserted': True,
                'no_engine_dispatch': True, 'output_still_poisoned': True,
                'firmware_code_unmodified': True}
    assert result['wfi_return_address'] == 0x101424
    assert '0x101a9c' in consumer and '0x101a9e' not in consumer
    assert not result['task_interrupts']
    context = word(0x1862bc)+40
    internal, network = word(context), word(context+8)
    expected_network = (ROOT/'results/windows-final-backend-output/kind-3.bin').read_bytes()
    assert hashlib.sha256(expected_network).hexdigest() == '86f4866d1a6a859b697a8a97548080bcfdc3ef21f06e2f9cb90efc44b75ae781'
    assert bytes(u.mem_read(network, 256)) == expected_network
    assert word(internal+40) == word(0x1862a4) == 1
    assert word(HOST+0x6060) == 0 and word(HOST+0x6440) == 1
    for index, address in enumerate(addresses[:7]):
        assert (0x2505c+8*index, 4, address & 0xffffffff) in result['scheduler_mmio_writes']
        assert (0x25060+8*index, 4, address >> 32) in result['scheduler_mmio_writes']
    irq = 0x400
    before_returns = len(result['trap_returns'])
    assert word(MMIO+0xd28) & irq
    u.mem_write(MMIO+0x3054, struct.pack('<I', engine_status))
    u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | irq))
    status = u.reg_read(rv.UC_RISCV_REG_MSTATUS)
    u.reg_write(rv.UC_RISCV_REG_MSTATUS, (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
    u.reg_write(rv.UC_RISCV_REG_MCAUSE, 0x8000000b)
    u.reg_write(rv.UC_RISCV_REG_MEPC, 0x101424)
    u.emu_start(u.reg_read(rv.UC_RISCV_REG_MTVEC), 0x40000000, count=200000)
    completed = bool(engine_status & 1)
    assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
    assert len(result['trap_returns']) == before_returns+1
    assert not word(MMIO+8) & irq
    assert word(internal+40) == word(0x1862a4) == word(0x180114) == int(not completed)
    assert word(HOST+0x6060) == int(completed)
    assert len(result['task_interrupts']) == int(completed)
    assert bytes(u.mem_read(layout[1]['address'], 3136)) == input_data
    assert bytes(u.mem_read(layout[2]['address'], 3136)) == b'\xa5'*3136
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    return {'case': 'original_umd_kmd_firmware_chain', 'umd_flags': umd_flags,
            'modeled_engine_status': engine_status, 'completed': completed,
            'umd_prefence_written': True, 'engine_addresses_verified': 7,
            'output_still_poisoned': True, 'firmware_code_unmodified': True,
            'kmd': report}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = []
    for seq in (0, 1, 31, 32, 0x80000000, 0xffffffff):
        for flags in (0, 128):
            _, _, _, result = build(pe, sequence=seq, job_flags=flags)
            cases.append(result)
    for length in (0, 1, 63, 65, 128, 0x400000):
        _, _, _, result = build(pe, length=length)
        cases.append(result)
    for options in ({'metadata': False}, {'disable_pre': True}, {'context': True}):
        _, _, _, result = build(pe, **options)
        cases.append(result)
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, RBASE)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    for flags in (0, 256):
        for engine_status in (0, 1, 4, 5):
            cases.append(firmware_chain(runtime, pe, firmware, model, engine_status, flags))
    for flags in (1, 7, 255):
        cases.append(firmware_chain(runtime, pe, firmware, model, None, flags))
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'kmd_sha256': KMD_SHA256, 'builder_rva': '0xdd7460',
                      'runtime_sha256': RUNTIME_HASH, 'firmware_sha256': PROFILE1_HASH,
                      'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'cases': cases, 'limitations': [
                          'Constructed submission/job/mapping/fence-provider objects',
                          'Engine-property getter, context memset and logging are modeled',
                          'Optional profiling gate is disabled; no OS submission or physical DMA',
                          'Firmware chains use RAM DMA/MMIO; arithmetic paths inject synthetic engine IRQ/status',
                          'No engine arithmetic or physical memory-reclamation guarantee',
                          'Firmware-v3 user-command list differs from native v2 address-list transport']}, indent=2))


if __name__ == '__main__':
    main()
