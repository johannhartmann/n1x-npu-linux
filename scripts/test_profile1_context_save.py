#!/usr/bin/env python3
"""Exercise original firmware context saving with explicit synthetic engine status."""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as rv

from test_profile1_active_preemption import start_task
from inspect_runtime_command import image, RUNTIME_HASH, BASE as RBASE
from inspect_kmd_task_submission import ROOT, KMD_SHA256
from emulate_profile1_commands import MMIO, PROFILE1_HASH
from emulate_firmware_commands import HOST


def check(runtime, pe, firmware, model, ready=1, flags=0, irq=0x400, checkpoint=True):
    f = start_task(runtime, pe, firmware, model, job_flags=flags, save_context=checkpoint)
    u, result, word = f['u'], f['result'], f['word']
    events, writes, execution_status, predicates = [], [], [], []
    host_before = bytes(u.mem_read(HOST, len(f['payload'])))

    def code(_, pc, size, user):
        if pc in (0x105a08, 0x105a42, 0x105a6a, 0x105aa4, 0x10127c, 0x101280, 0x101990,
                  0x1059d8, 0x1059ce, 0x105f70, 0x105cd8):
            events.append([hex(pc), *[hex(u.reg_read(getattr(rv, 'UC_RISCV_REG_'+r)))
                                     for r in ('A0', 'A1', 'A2')]])
        if pc == 0x101990:
            value = u.reg_read(rv.UC_RISCV_REG_A0)
            execution_status.append(value if value < 1 << 31 else value-(1 << 32))
        if pc == 0x102800:
            instruction = bytes(u.mem_read(u.reg_read(rv.UC_RISCV_REG_A0), 16))
            if int.from_bytes(instruction[4:8], 'little') == 0x25044:
                predicates.append([instruction.hex(), u.reg_read(rv.UC_RISCV_REG_A1)])

    def write(_, access, address, size, value, user):
        if address == MMIO+0x118 and value & 0x20:
            transfer = result['dma_commands'][-1]
            target, size = transfer['destination'], transfer['bytes']
            writes.append({'destination': hex(target), 'bytes': size,
                           'data': bytes(u.mem_read(target, size)).hex()})

    def interrupt(irq):
        assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        assert word(MMIO+0xd28) & irq
        u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | irq))
        status = u.reg_read(rv.UC_RISCV_REG_MSTATUS)
        u.reg_write(rv.UC_RISCV_REG_MSTATUS,
                    (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
        u.reg_write(rv.UC_RISCV_REG_MCAUSE, 0x8000000b)
        u.reg_write(rv.UC_RISCV_REG_MEPC, u.reg_read(rv.UC_RISCV_REG_RA))
        returns = len(result['trap_returns'])
        u.emu_start(u.reg_read(rv.UC_RISCV_REG_MTVEC), 0x40000000, count=200000)
        assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        assert len(result['trap_returns']) == returns+1
        assert not word(MMIO+8) & irq

    def snapshot():
        return {'pc': hex(u.reg_read(rv.UC_RISCV_REG_PC)), 'state': word(0x1862a4),
                'remaining': word(f['internal']+40), 'save_remaining': word(f['internal']+0x48),
                'restore_remaining': word(f['internal']+0x4c),
                'queue': word(0x180114), 'done': word(HOST+0x6060),
                'context': bytes(u.mem_read(f['context_buffer'], 32)).hex(),
                'notifications': len(f['notifications'])}

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    u.mem_write(MMIO+0x25044, struct.pack('<I', ready))
    u.mem_write(MMIO+0x68, struct.pack('<I', 0x212))
    u.mem_write(MMIO+0x64, struct.pack('<I', (HOST+f['header']) >> 8))
    interrupt(4)
    states = [snapshot()]
    failed = not (ready & 15)
    for step, status in enumerate((4,) if failed else (4, 0, 0)):
        u.mem_write(MMIO+0x3054, struct.pack('<I', status))
        try:
            interrupt(irq)
        except AssertionError:
            if checkpoint or failed or step != 2:
                raise
            # The harness deliberately refuses DMA outside mapped host backing.
            # Prove this is the original checkpoint write to address zero, not
            # an unrelated assertion in setup, IRQ handling or state checking.
            assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x100206
            address = (word(MMIO+0x128) << 40)+(word(MMIO+0x110) << 8)+word(MMIO+0x11c)
            assert address == 0 and events[-1] == ['0x1059ce', '0x185ce4', '0x0', '0x0']
            assert not writes and len(f['notifications']) == 2
            assert bytes(u.mem_read(HOST, len(f['payload']))) == host_before
            assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
            return {'missing_checkpoint': True, 'engine_irq': hex(irq),
                    'blocked_dma_address': 0, 'firmware_code_unmodified': True,
                    'blocked_by': 'RAM-DMA harness mapping boundary, not firmware validation',
                    'events': events}
        states.append(snapshot())
    assert checkpoint, 'Missing-checkpoint test did not reach the expected DMA boundary'
    saved = struct.pack('<QQQQ', 0, 0, 1800, 1)
    assert [s['state'] for s in states] == ([2, 3] if failed else [2, 3, 3, 0])
    assert [s['queue'] for s in states] == ([1, 0] if failed else [1, 1, 1, 0])
    assert [s['save_remaining'] for s in states] == ([2, 2] if failed else [2, 2, 1, 0])
    assert all(s['done'] == 0 and s['remaining'] == 1 for s in states)
    assert all(s['context'] == bytes(32).hex() for s in states[:-1])
    assert states[-1]['context'] == (bytes(32) if failed else saved).hex()
    assert [s['notifications'] for s in states] == ([2, 3] if failed else [2, 2, 2, 3])
    assert execution_status == ([-16] if failed else [0])
    assert not result['task_interrupts']
    expected_host = bytearray(host_before)
    if not failed:
        expected_host[0xc000:0xc020] = saved
    struct.pack_into('<I', expected_host, f['fence'], 8)
    assert bytes(u.mem_read(HOST, len(f['payload']))) == expected_host
    assert [n['mailbox'] for n in f['notifications']] == [[0x1c02, 1], [0x702, HOST >> 8], [0x120b, 0]]
    assert f['notifications'][-1]['outstanding'] == 1
    assert bytes(u.mem_read(0x183720, 1)) == b'\0'
    save_writes = list(writes)
    expected_save_writes = ([] if failed else [
        {'destination': hex(f['context_buffer']+offset), 'bytes': 4,
         'data': saved[offset:offset+4].hex()} for offset in range(0, 32, 4)])
    expected_save_writes.extend({'destination': hex(HOST+f['fence']), 'bytes': 4,
                                 'data': struct.pack('<I', value).hex()} for value in (5, 8))
    assert save_writes == expected_save_writes
    resumed = []
    if not failed:
        u.mem_write(MMIO+0x68, struct.pack('<I', 0x307))
        u.mem_write(MMIO+0x64, struct.pack('<I', HOST >> 8))
        interrupt(4)
        resumed.append(snapshot())
        for status in (0, 0, 1):
            u.mem_write(MMIO+0x3054, struct.pack('<I', status))
            interrupt(irq)
            resumed.append(snapshot())
        assert [s['state'] for s in resumed] == ([4, 4, 1, 0] if flags else [1, 1, 1, 0])
        assert [s['restore_remaining'] for s in resumed] == ([2, 1, 0, 0] if flags else [2]*4)
        assert [s['done'] for s in resumed] == [0, 0, 0, 1]
        assert [s['queue'] for s in resumed] == [1, 1, 1, 0]
        assert execution_status == [0, 0]
        assert len(result['task_interrupts']) == 1
        assert [n['mailbox'] for n in f['notifications']] == [
            [0x1c02, 1], [0x702, HOST >> 8], [0x120b, 0], [0x702, HOST >> 8]]
        if flags:
            struct.pack_into('<Q', expected_host, 0xc018, 0)
        struct.pack_into('<I', expected_host, 0x6060, 1)
        assert bytes(u.mem_read(HOST, len(f['payload']))) == expected_host
        assert sum(e[0] == '0x105a6a' for e in events) == (2 if flags else 0)
        assert sum(e[0] == '0x1059d8' for e in events) == int(bool(flags))
    assert predicates and all(p == ['01040000445002000f00000000000000', ready & 15]
                              for p in predicates)
    assert bytes(u.mem_read(f['layout'][1]['address'], 3136)) == f['input']
    assert bytes(u.mem_read(f['layout'][2]['address'], 3136)) == b'\xa5'*3136
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    return {'ready': ready, 'job_flags': flags, 'states': states, 'events': events,
            'resumed': resumed, 'engine_irq': hex(irq), 'execution_status': execution_status,
            'context_save_failed': failed, 'predicates': predicates,
            'firmware_code_unmodified': True, 'output_still_poisoned': True,
            'writes': writes, 'notifications': f['notifications']}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, RBASE)
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    cases = [check(runtime, pe, firmware, model, ready, flags, irq)
             for ready in (0, 1, 15, 16, 17) for flags in (0, 128) for irq in (0x400, 0x800)]
    cases.extend(check(runtime, pe, firmware, model, irq=irq, checkpoint=False)
                 for irq in (0x400, 0x800))
    print(json.dumps({'passed': True, 'case_count': len(cases), 'cases': cases,
                      'kmd_sha256': KMD_SHA256, 'runtime_sha256': RUNTIME_HASH,
                      'firmware_sha256': PROFILE1_HASH,
                      'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'hardware_access': False,
                      'limitations': ['Constructed Windows context object and allocations',
                                      'Synthetic engine status, interrupts and synchronous RAM DMA',
                                      'Engine context transfers and arithmetic are not emulated',
                                      'No physical save/restore, numerical resume or DMA-quiescence proof']}, indent=2))


if __name__ == '__main__':
    main()
