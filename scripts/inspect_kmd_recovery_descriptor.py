#!/usr/bin/env python3
"""Execute the Windows recovery descriptor rewrite and original firmware consumer.

All pool/DMA memory and hardware registers are RAM models. The late-rewrite
case stops before the original command-list DMA; it does not run arithmetic.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as rv

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import (BASE, ROOT, OBJECT, PARENT, PRIMARY,
                                         OWNER, DESCRIPTOR, JOB, HOST, KMD_SHA256)
from inspect_kmd_command_descriptor import build
from emulate_profile1_commands import run, MMIO, ACK, PROFILE1_HASH

MANAGER, POOL = OWNER+0x6000, OWNER+0x1000


def initialize(pe, selector):
    m = DispatchMachine(pe)
    m.write(OBJECT, 'Q', BASE+0xe9c050)
    m.write(OBJECT+0x4648, 'I', selector)
    m.write(PARENT+0x6ec0, 'Q', OBJECT)
    m.write(OBJECT+0x1988, 'Q', POOL)
    m.write(OBJECT+0xee0, 'Q', PRIMARY)
    m.write(PRIMARY+0x10, 'III', 1, 0, 0x33)
    vt = 0xe9cb00 if selector == 0xe0000023 else 0xe9cb68
    size = 0xa000 if selector == 0xe0000023 else 0xd000
    entries, boundaries = [], []
    originals = (0x19e1580, 0x19e28b0, 0x19e2a40, 0x19e2a00,
                 0x19e2700, 0xdc24b8, 0x19e2750)

    def hook(_, pc, length, user):
        if pc-BASE in originals:
            entries.append(hex(pc-BASE))
        if pc == BASE+0xdc24e8:
            assert [m.reg(i) for i in range(4)] == [0x38, POOL, 0x200, 0x6c4c764e]
            boundaries.append('pool_allocate_0x38')
            m.ret(MANAGER)
        elif pc == BASE+0xdc80f0:
            assert [m.reg(i) for i in range(6)] == [OBJECT, size, 0, 2**64-1, 0, 2]
            boundaries.append('mapped_ring_allocate')
            m.ret(DESCRIPTOR)
        elif pc == BASE+0xdc82b0:
            assert [m.reg(i) for i in range(2)] == [OBJECT, DESCRIPTOR]
            boundaries.append('ring_device_address')
            m.ret(HOST)
        elif pc == BASE+0xcd148:
            assert [m.reg(i) for i in range(3)] == [DESCRIPTOR, 0, size]
            boundaries.append('zero_ring')
            m.u.mem_write(DESCRIPTOR, bytes(size))
            m.ret(DESCRIPTOR)
        elif pc == BASE+0xdf8f38:
            raise AssertionError(('vendor assertion', hex(m.reg(30)-BASE)))

    hook_id = m.u.hook_add(UC_HOOK_CODE, hook)
    assert m.call(0x19e1580, PARENT) == 1
    assert m.read(PARENT+0x8680, 'Q') == (MANAGER,)
    assert m.read(MANAGER, 'Q') == (BASE+vt,)
    assert m.read(MANAGER+0x18, 'I') == (0x500,)
    assert m.read(MANAGER+0x20, 'QQQ') == (PARENT, HOST, DESCRIPTOR)
    assert struct.unpack('<Q', pe.get_data(vt+0x18, 8)) == (BASE+0xdd6dd0,)
    expected = [0x19e1580, 0x19e28b0]
    if selector != 0xe0000023:
        expected.append(0x19e2a40)
    expected += [0x19e2a00, 0x19e2700, 0xdc24b8, 0x19e2750]
    assert entries == [hex(x) for x in expected]
    m.u.hook_del(hook_id)
    return m, dict(selector=hex(selector), vtable=hex(vt), ring_bytes=size,
                   original_creation_path=entries, modeled_boundaries=boundaries)


def rewrite(m, descriptor, sequence):
    assert len(descriptor) == 0x500
    offset = (sequence & 31)*0x500
    m.u.mem_write(DESCRIPTOR+offset, descriptor)
    m.write(JOB+0x2c, 'I', sequence)
    before = bytes(m.u.mem_read(DESCRIPTOR, 0x10000))
    entries = []

    def hook(_, pc, length, user):
        if pc-BASE in (0xdd6440, 0xdd6dd0, 0xdd6638):
            entries.append(hex(pc-BASE))
        elif pc == BASE+0xdf8f38:
            raise AssertionError(('descriptor sequence mismatch', hex(m.reg(30)-BASE)))

    hook_id = m.u.hook_add(UC_HOOK_CODE, hook)
    try:
        assert m.call(0xdd6440, PARENT, JOB) == 0
    finally:
        m.u.hook_del(hook_id)
    expected = bytearray(before)
    struct.pack_into('<QH', expected, offset+0x15, 0xdeadf000, 0)
    flags = struct.unpack_from('<H', descriptor, 0x29)[0]
    struct.pack_into('<H', expected, offset+0x29, flags | 1)
    assert bytes(m.u.mem_read(DESCRIPTOR, 0x10000)) == expected
    assert entries == ['0xdd6440', '0xdd6dd0', '0xdd6638']
    return bytes(expected[offset:offset+0x500])


def firmware_consumer(firmware, original, rewritten, ctx, ctxdata, late):
    payload = bytearray(0xe000)
    payload[:0x500] = original if late else rewritten
    payload[ctx-HOST:ctx-HOST+len(ctxdata)] = ctxdata
    for offset, value in ((0x6060, 0), (0x6440, 0xdeadbeef), (0x6840, 0xfeed)):
        struct.pack_into('<I', payload, offset, value)
    events, live = [], []

    def response(u):
        live[:] = [u]
        def observe(_, pc, length, user):
            if pc == 0x1016b6 and late:
                # Execution descriptor DMA has completed; change only host RAM.
                cached = u.reg_read(rv.UC_RISCV_REG_S4)
                assert bytes(u.mem_read(cached, 60)) == original[:60]
                u.mem_write(HOST, rewritten)
                events.append('rewrite_after_execution_header_dma')
            elif pc == 0x104ed8:
                source = u.reg_read(rv.UC_RISCV_REG_A0) | u.reg_read(rv.UC_RISCV_REG_A1) << 32
                if source == HOST+0x1000:
                    events.append('original_command_dma_requested')
                    u.emu_stop()
                assert source != 0xdeadf000
        u.hook_add(UC_HOOK_CODE, observe)
        for offset in (8, 0xd28):
            value = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
            u.mem_write(MMIO+offset, struct.pack('<I', value & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(firmware, method=0x307, host_payload=payload, host_handler=response,
                 boot_from_reset=True, via_trap=True, dma_register_model=True,
                 resume_scheduler=True)
    assert result['returned'] and not result['function_stubs']
    assert result['firmware_code_unmodified']
    assert result['interrupts'] == [(0x702, HOST >> 8)]
    u = live[0]
    assert bytes(u.mem_read(HOST, 0x500)) == rewritten
    word = lambda address: struct.unpack('<I', u.mem_read(address, 4))[0]
    if late:
        assert events == ['rewrite_after_execution_header_dma', 'original_command_dma_requested'], events
        assert result['pc'] == '0x104ed8' and not result['scheduler_wfi']
        assert word(HOST+0x6060) == 0 and not result['task_interrupts']
    else:
        assert not events and result['scheduler_wfi']
        assert word(HOST+0x6060) == word(HOST+0x6440) == 1
        assert word(0x180114) == word(0x1862a4) == 0
        assert len(result['task_interrupts']) == 1
        assert not any(0x2505c <= a <= 0x25090 for a, _, _ in result['scheduler_mmio_writes'])
    return dict(late_host_rewrite=late, events=events, stop_pc=result['pc'],
                completion_word=word(HOST+0x6060), task_interrupts=len(result['task_interrupts']),
                firmware_code_unmodified=True, dma_transfers=len(result['dma_commands']))


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = []
    for selector in (0xe0000023, 0xe0000025, 0xe0000026):
        m, report = initialize(pe, selector)
        report['sequences'] = []
        for sequence in (0, 1, 31, 32, 0xffff):
            descriptor, _, _, _ = build(pe, sequence=sequence)
            rewritten = rewrite(m, descriptor, sequence)
            assert rewrite(m, rewritten, sequence) == rewritten
            report['sequences'].append(sequence)
        cases.append(report)
    rejected = []
    for descriptor_sequence, job_sequence in ((1, 2), (0x10000, 0x10000)):
        m, _ = initialize(pe, 0xe0000025)
        descriptor, _, _, _ = build(pe, sequence=descriptor_sequence)
        try:
            rewrite(m, descriptor, job_sequence)
        except AssertionError as error:
            assert error.args == (('descriptor sequence mismatch', '0xdd6e00'),)
        else:
            raise AssertionError('sequence mismatch did not reach vendor assertion')
        slot = DESCRIPTOR+(job_sequence & 31)*0x500
        assert bytes(m.u.mem_read(slot, 0x500)) == descriptor
        rejected.append(dict(descriptor_sequence=descriptor_sequence,
                             job_sequence=job_sequence, stopped_at_vendor_assertion=True))
    m, _ = initialize(pe, 0xe0000025)
    descriptor, ctx, ctxdata, _ = build(pe, command=HOST+0x1000)
    rewritten = rewrite(m, descriptor, 1)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    consumer = [firmware_consumer(firmware, descriptor, rewritten, ctx, ctxdata, late)
                for late in (False, True)]
    print(json.dumps(dict(passed=True, hardware_access=False, kmd_sha256=KMD_SHA256,
        firmware_sha256=PROFILE1_HASH, creation_cases=cases, firmware_cases=consumer,
        assertion_cases=rejected,
        limitations=['Synthetic adapter/node state and pool/DMA allocation boundaries',
                     'Original descriptor and firmware instructions; RAM-only DMA/MMIO',
                     'Late rewrite stops at command DMA request, before command execution',
                     'No running-task cancellation, physical DMA drain or safe reclamation proven']), indent=2))


if __name__ == '__main__':
    main()
