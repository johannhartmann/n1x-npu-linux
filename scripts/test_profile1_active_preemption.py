#!/usr/bin/env python3
"""Original Windows-built ReLU and preemption requests in the RV32 scheduler.

CPU code is original; host allocations, MMIO, IRQ delivery and DMA are modeled.
No arithmetic execution or physical DMA quiescence is established here.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_WRITE
from unicorn import riscv_const as rv

from inspect_kmd_command_descriptor import build as build_task
from inspect_kmd_preemption_request import build as build_preempt
from inspect_runtime_command import Builder, image, RUNTIME_HASH, BASE as RBASE, MEM, CPU, OUT
from inspect_kmd_task_submission import ROOT, KMD_SHA256
from diagnostic_fixture import build_relu
from emulate_profile1_commands import run, MMIO, ACK, PROFILE1_HASH
from emulate_firmware_commands import HOST


def start_task(runtime, pe, firmware, model, *, enabled=True, job_flags=0, save_context=False):
    """Shared RAM fixture; stop after original firmware dispatches the engine."""
    backing, addresses, layout = build_relu(model, HOST+0x10000)
    payload = bytearray(0x10000)+backing
    b = Builder(runtime)
    b.gpu[MEM] = HOST+0x1000
    b.addresses([(0, address) for address in addresses])
    b.fences([(4, 1, HOST+0x7000, 0x11223344)])
    assert b.build(flags=0, field=0) == 0
    payload[0x1000:0x5000] = b.u.mem_read(CPU, 0x4000)
    descriptor, ctx, ctxdata, _ = build_task(pe, command=b.get(OUT), job_flags=job_flags,
                                          context=save_context, context_address=HOST+0xc000)
    payload[:len(descriptor)] = descriptor
    payload[ctx-HOST:ctx-HOST+len(ctxdata)] = ctxdata
    for offset, value in ((0x6060, 0), (0x6440, 0xdeadbeef),
                          (0x6840, 0xfeed), (0x7000, 0xdeadbeef)):
        struct.pack_into('<I', payload, offset, value)
    preempt, header, post, fence = build_preempt(pe)
    for offset, size in ((header, 32), (post, 27), (fence, 4)):
        assert 0x8000 <= offset and offset+size <= 0x10000
        assert offset+size <= ctx-HOST or offset >= ctx-HOST+len(ctxdata)
        payload[offset:offset+size] = preempt[offset:offset+size]
    input_data = (ROOT/'results/relu-probe/input-chw16-fp16.bin').read_bytes()
    assert hashlib.sha256(input_data).hexdigest() == '73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45'
    for memory, values in ((1, input_data), (2, b'\xa5'*3136)):
        start = 0x10000+layout[memory]['offset']
        payload[start:start+3136] = values
    live, notifications = [], []
    commands = [(0x31c, int(enabled)), (0x307, HOST >> 8)]

    def word(u, address):
        return int.from_bytes(u.mem_read(address, 4), 'little')

    def submit(u):
        command, data = commands[len(live)]
        live.append(u)
        u.mem_write(MMIO+0x68, struct.pack('<II', command, data))

    def ack(u):
        notifications.append({'mailbox': list(struct.unpack('<II', u.mem_read(MMIO+0x40, 8))),
                              'state': word(u, 0x1862a4),
                              'outstanding': int.from_bytes(u.mem_read(0x183720, 1), 'little'),
                              'preempt_fence': word(u, HOST+fence),
                              'task_done': word(u, HOST+0x6060)})
        for offset in (8, 0xd28):
            u.mem_write(MMIO+offset, struct.pack('<I', word(u, MMIO+offset) & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(firmware, sequence=[0, 0], host_payload=payload, host_submit=submit,
                 host_handler=ack, boot_from_reset=True, via_trap=True,
                 dma_register_model=True, resume_scheduler=True)
    u = live[-1]
    assert result['returned'] and result['scheduler_wfi']
    assert result['wfi_return_address'] == 0x101424
    assert result['function_stubs'] == [] and result['firmware_code_unmodified']
    assert word(u, 0x1862a4) == word(u, 0x180114) == 1
    assert word(u, HOST+0x6060) == 0 and word(u, HOST+0x6440) == 1
    assert bytes(u.mem_read(0x183728, 1)) == bytes([int(enabled)])
    assert not result['task_interrupts']
    for index, address in enumerate(addresses[:7]):
        assert (0x2505c+8*index, 4, address & 0xffffffff) in result['scheduler_mmio_writes']
        assert (0x25060+8*index, 4, address >> 32) in result['scheduler_mmio_writes']
    internal = word(u, word(u, 0x1862bc)+40)
    assert word(u, internal+40) == 1
    return {'u': u, 'result': result, 'layout': layout, 'input': input_data,
            'notifications': notifications, 'word': lambda address: word(u, address),
            'payload': payload, 'header': header, 'fence': fence, 'internal': internal,
            'context_header': ctx, 'context_buffer': HOST+0xc000}


def check(runtime, pe, firmware, model, engine_status=None, *,
          method_flags=0x200, engine_irq=0x400, enabled=True):
    fixture = start_task(runtime, pe, firmware, model, enabled=enabled)
    u, result = fixture['u'], fixture['result']
    layout, input_data = fixture['layout'], fixture['input']
    notifications, payload = fixture['notifications'], fixture['payload']
    header, fence, internal = fixture['header'], fixture['fence'], fixture['internal']

    def word(u, address):
        return int.from_bytes(u.mem_read(address, 4), 'little')

    events, accesses, post_status, fence_writes, execution_status = [], [], [], [], []

    def code(_, pc, size, user):
        if pc in (0x105b90, 0x105f70, 0x105cd8, 0x105d2e, 0x102d7a):
            events.append([hex(pc), u.reg_read(rv.UC_RISCV_REG_A0), u.reg_read(rv.UC_RISCV_REG_A1)])
        if pc == 0x105d2e:
            post_status.append(u.reg_read(rv.UC_RISCV_REG_A0))
        if pc == 0x101990:  # original arithmetic executor returns to the UMD consumer
            value = u.reg_read(rv.UC_RISCV_REG_A0)
            execution_status.append(value if value < 1 << 31 else value-(1 << 32))

    def access(_, kind, address, size, value, user):
        if MMIO <= address < MMIO+0x40000:
            accesses.append([kind, address-MMIO, size,
                             value if kind == UC_MEM_WRITE else word(u, address)])
        if kind == UC_MEM_WRITE and address == MMIO+0x118 and value & 0x20:
            # The register DMA backend is installed first and has copied RAM.
            transfer = result['dma_commands'][-1]
            assert transfer['direction'] == 'write' and transfer['bytes'] == 4
            destination = transfer['destination']
            fence_writes.append([destination, word(u, destination)])

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, access)
    dma_before = len(result['dma_commands'])
    host_before = bytes(u.mem_read(HOST, len(payload)))

    def interrupt(irq):
        assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        assert word(u, MMIO+0xd28) & irq
        # Explicit hardware interrupt entry, preserving the original WFI caller.
        u.mem_write(MMIO+8, struct.pack('<I', word(u, MMIO+8) | irq))
        status = u.reg_read(rv.UC_RISCV_REG_MSTATUS)
        u.reg_write(rv.UC_RISCV_REG_MSTATUS,
                    (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
        u.reg_write(rv.UC_RISCV_REG_MCAUSE, 0x8000000b)
        u.reg_write(rv.UC_RISCV_REG_MEPC, u.reg_read(rv.UC_RISCV_REG_RA))
        returns = len(result['trap_returns'])
        u.emu_start(u.reg_read(rv.UC_RISCV_REG_MTVEC), 0x40000000, count=200000)
        assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        assert len(result['trap_returns']) == returns+1
        assert not word(u, MMIO+8) & irq

    u.mem_write(MMIO+0x68, struct.pack('<I', method_flags | 18))
    u.mem_write(MMIO+0x64, struct.pack('<I', (HOST+header) >> 8))
    interrupt(4)
    requested = {'pc': hex(u.reg_read(rv.UC_RISCV_REG_PC)),
                 'state': word(u, 0x1862a4), 'task_done': word(u, HOST+0x6060),
                 'preempt_fence': word(u, HOST+fence)}
    assert requested == {'pc': '0x102194', 'state': 2 if enabled else 1,
                         'task_done': 0, 'preempt_fence': 0xa5a5a5a5}
    assert bytes(u.mem_read(HOST, len(payload))) == host_before
    assert word(u, 0x180114) == 1
    assert int.from_bytes(u.mem_read(0x183720, 1), 'little') == int(enabled)
    request_engine_writes = [a[1:] for a in accesses if a[0] == UC_MEM_WRITE and a[1] >= 0x2000]
    assert request_engine_writes == ([[0x3018, 4, 0x7ff98], [0x25058, 4, 1]] if enabled else [])
    assert len(result['dma_commands'])-dma_before == (2 if enabled else 0)
    expected_mailboxes = [[0x1c02, int(enabled)], [0x702, HOST >> 8]]
    if not enabled:
        expected_mailboxes.append([0xc1201, (HOST+header) >> 8])
    elif method_flags & 0x100:
        expected_mailboxes.append([0x1202, (HOST+header) >> 8])
    assert [n['mailbox'] for n in notifications] == expected_mailboxes
    if engine_status is not None:
        assert enabled
        u.mem_write(MMIO+0x3054, struct.pack('<I', engine_status))
        interrupt(engine_irq)
    completed = engine_status is not None and engine_status & 3 == 1
    save_failed = engine_status is not None and bool(engine_status & 4) and not completed
    notified = completed or save_failed
    final_state = 0 if completed else 3 if save_failed else 2 if enabled else 1
    expected_host = bytearray(host_before)
    if notified:
        struct.pack_into('<I', expected_host, fence, 8)
        expected_mailboxes.append([0x120b, 0])
    if completed:
        struct.pack_into('<I', expected_host, 0x6060, 1)
    assert bytes(u.mem_read(HOST, len(payload))) == expected_host
    expected_writes = [[HOST+0x6060, 1]] if completed else []
    if notified:
        expected_writes.extend([[HOST+fence, 5], [HOST+fence, 8]])
    assert fence_writes == expected_writes
    assert [n['mailbox'] for n in notifications] == expected_mailboxes
    assert post_status == ([0] if notified else [])
    assert execution_status == ([-16] if save_failed else [0] if completed else [])
    assert not result['task_interrupts']
    assert not any(a[0] == UC_MEM_WRITE and a[1] == 0 and a[3] & 64 for a in accesses)
    if notified:
        assert notifications[-1] == {'mailbox': [0x120b, 0], 'state': final_state,
                                     'outstanding': 1, 'preempt_fence': 8,
                                     'task_done': int(completed)}
    assert word(u, 0x1862a4) == final_state
    assert word(u, 0x180114) == int(not notified)
    assert word(u, internal+40) == int(not completed)
    assert int.from_bytes(u.mem_read(0x183720, 1), 'little') == int(enabled and not notified)
    assert bytes(u.mem_read(layout[1]['address'], 3136)) == input_data
    assert bytes(u.mem_read(layout[2]['address'], 3136)) == b'\xa5'*3136
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    return {'pc': hex(u.reg_read(rv.UC_RISCV_REG_PC)), 'events': events,
            'after_request': requested, 'engine_status': engine_status,
            'engine_irq': hex(engine_irq), 'method': hex(method_flags | 18),
            'enabled': enabled, 'completed': completed, 'context_save_failed': save_failed,
            'execution_status': execution_status,
            'postaction_status': post_status, 'request_engine_writes': request_engine_writes,
            'fence_writes': fence_writes,
            'task_interrupt_count': len(result['task_interrupts']),
            'swgen0_writes': 0,
            'notifications': notifications, 'state': word(u, 0x1862a4),
            'outstanding': int.from_bytes(u.mem_read(0x183720, 1), 'little'),
            'queue_count': word(u, 0x180114), 'task_done': word(u, HOST+0x6060),
            'preempt_fence': word(u, HOST+fence),
            'input_unchanged': True, 'output_still_poisoned': True,
            'firmware_code_unmodified': True,
            'dma': result['dma_commands'][dma_before:]}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, RBASE)
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    rows = [check(runtime, pe, firmware, model, status, method_flags=flags, engine_irq=irq)
            for status in (None, *range(8)) for flags in (0x200, 0x300)
            for irq in (0x400, 0x800)]
    rows.extend(check(runtime, pe, firmware, model, method_flags=flags, enabled=False)
                for flags in (0x200, 0x300))
    print(json.dumps({'passed': True, 'case_count': len(rows), 'cases': rows,
                      'kmd_sha256': KMD_SHA256, 'runtime_sha256': RUNTIME_HASH,
                      'firmware_sha256': PROFILE1_HASH,
                      'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'hardware_access': False,
                      'limitations': ['Synthetic allocations, MMIO, engine IRQ/status and DMA',
                                      'No arithmetic execution, physical DMA quiescence or memory reclamation',
                                      'Zero checkpoint pointer; save predicate fails before checkpoint write',
                                      'No restore/resume tested by this script']}, indent=2))


if __name__ == '__main__':
    main()
