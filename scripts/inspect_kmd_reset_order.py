#!/usr/bin/env python3
"""Execute the complete outer DLA engine-reset body with explicit OS/RM boundaries.

The recovery helper and its context/descriptor callbacks execute original code.
OS synchronization is a synchronous RAM model; firmware reset/reload is a
recording result boundary already studied separately, not physical recovery.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_WRITE
from unicorn import arm64_const as r

from inspect_kmd_recovery import replay, CONTEXT
from inspect_kmd_task_submission import (BASE, ROOT, OBJECT, PARENT, PRIMARY,
                                         OWNER, DESCRIPTOR, STUB, STACK, KMD_SHA256)


def consume_deferred(m, pe, completion):
    """Run one original worker iteration, stopping before completion notification."""
    queue = PARENT+0x7f08
    m.write(queue, 'Q', OBJECT)
    m.write(DESCRIPTOR+0x64, 'I', completion)
    imports = {e.address-BASE: e.name.decode()
               for d in pe.DIRECTORY_ENTRY_IMPORT for e in d.imports if e.name}
    boundaries, events, writes = {}, [], []
    for i, (iat, name) in enumerate(((0xe3e2d8, 'KeGetCurrentThread'),
            (0xe3e610, 'KeSetPriorityThread'), (0xe3e9e8, 'KeWaitForSingleObject'),
            (0xe3e9e0, 'KeClearEvent'), (0xe3e0c8, 'KeAcquireSpinLockRaiseToDpc'),
            (0xe3e0d0, 'KeReleaseSpinLock'))):
        assert imports[iat] == name
        stub = STUB+0x300+i*4
        m.write(BASE+iat, 'Q', stub)
        boundaries[stub] = name

    def hook(_, pc, size, user):
        if pc in boundaries:
            name = boundaries[pc]
            events.append(name)
            if name == 'KeWaitForSingleObject':
                assert [m.reg(i) for i in range(5)] == [queue+0x728, 0, 0, 0, 0]
            elif name == 'KeSetPriorityThread':
                assert [m.reg(i) for i in range(2)] == [OWNER+0xb000, 29]
            elif name == 'KeClearEvent':
                assert m.reg(0) in (queue+0x728, queue+0x740)
            else:
                assert name == 'KeGetCurrentThread' or m.reg(0) == queue+8
            m.ret(OWNER+0xb000 if name == 'KeGetCurrentThread' else 0)
        elif pc == BASE+0xe3d040:
            destination, source, length = [m.reg(i) for i in range(3)]
            assert queue+0x28 <= destination < source < queue+0x28+3*0x38
            assert length in (0x38, 0x70)
            assert source+length <= queue+0x28+3*0x38
            m.u.mem_write(destination, bytes(m.u.mem_read(source, length)))
            m.ret(destination)
        elif pc == BASE+0xcd148:
            assert m.reg(0) == queue+0x28 and m.reg(1) == 0 and m.reg(2) == 0x38
            m.u.mem_write(m.reg(0), bytes(0x38))
            m.ret(m.reg(0))

    def write(_, access, address, size, value, user):
        writes.append([hex(address-DESCRIPTOR), size, value,
                       hex(m.u.reg_read(r.UC_ARM64_REG_PC)-BASE)])
    hook_id = m.u.hook_add(UC_HOOK_CODE, hook)
    write_id = m.u.hook_add(UC_HOOK_MEM_WRITE, write, begin=DESCRIPTOR, end=DESCRIPTOR+0xfff)
    m.u.reg_write(r.UC_ARM64_REG_X0, queue)
    m.u.reg_write(r.UC_ARM64_REG_SP, STACK+0xf000)
    m.u.reg_write(r.UC_ARM64_REG_X30, STUB+0xff0)
    m.u.emu_start(BASE+0xe16cf0, BASE+0xe17194, count=50000)
    assert m.u.reg_read(r.UC_ARM64_REG_PC) == BASE+0xe17194
    m.u.hook_del(hook_id)
    m.u.hook_del(write_id)
    assert events.count('KeWaitForSingleObject') == 1
    assert events[-1] == 'KeReleaseSpinLock'
    expected = []
    if completion < 3:
        for value in range(completion+1, 4):
            expected += [['0x444', 4, value, '0xe16e44'],
                         ['0x64', 4, value, '0xe16ed0']]
    assert writes == expected, writes
    assert m.read(queue+0x24, 'I') == (3 if completion == 0xffffffff else 0,)
    final = completion if completion == 0xffffffff else 3
    assert m.read(DESCRIPTOR+0x64, 'I') == (final,)
    return dict(initial_completion=completion, final_completion=final,
                remaining_records=m.read(queue+0x24, 'I')[0], semaphore_writes=writes,
                stop_before='0xe17194', modeled_os_calls=events)


def run(pe, count=3, reload_success=True, reject=False, capability=True,
        worker_completion=None):
    def execute(m, request, jobs):
        def missing(u, access, address, size, value, user):
            if BASE <= address < BASE+pe.OPTIONAL_HEADER.SizeOfImage:
                page = address & ~4095
                u.mem_map(page, 4096)
                u.mem_write(page, pe.get_data(page-BASE, 4096))
                return True
            return False
        m.u.hook_add(UC_HOOK_MEM_UNMAPPED, missing)
        m.write(OBJECT, 'Q', BASE+0xe9c050)
        m.write(OBJECT+0x52e0, 'Q', PARENT)
        m.write(PARENT, 'Q', BASE+0xe9c910)
        m.write(PARENT+0x6ec0, 'Q', OBJECT)
        m.write(OBJECT+0x1c8, 'Q', PRIMARY+0x6000)
        m.write(PRIMARY+0x6000, 'II', int(capability) << 28, 0)
        m.write(OBJECT+0xee0, 'Q', PRIMARY+0x7000)
        m.write(PRIMARY+0x7010, 'I', 4)
        m.write(OBJECT+0x480c, 'B', 1)
        m.write(OWNER+0x1000+0x520, 'III', 0x200, 256, 4)
        m.write(OWNER+0x1000+0x514, 'III', 0x300, 256, 4)
        m.write(CONTEXT+0x78, 'Q', OWNER+0x5000)
        m.write(OWNER+0x5000, 'Q', BASE+0xea0f08)
        m.write(OWNER+0x5000+0x98, 'Q', OWNER+0x9000)
        for job in jobs:
            m.write(job+8, 'Q', OWNER+0x5000)
        imports = {e.address-BASE: e.name.decode()
                   for d in pe.DIRECTORY_ENTRY_IMPORT for e in d.imports if e.name}
        boundaries = {}
        for i, (iat, name) in enumerate(((0xe3e000, 'KeGetCurrentIrql'),
                (0xe3e0c8, 'KeAcquireSpinLockRaiseToDpc'), (0xe3e0d0, 'KeReleaseSpinLock'),
                (0xe3e0b8, 'KeSetEvent'), (0xe3e008, 'KeQueryPerformanceCounter'))):
            assert imports[iat] == name
            stub = STUB+0x100+i*4
            m.write(BASE+iat, 'Q', stub)
            boundaries[stub] = name
        pending, stages, writes, signals = [], [], [], []
        irql = 0

        def hook(_, pc, size, user):
            nonlocal irql
            if pc in boundaries:
                name = boundaries[pc]
                if name == 'KeSetEvent':
                    assert m.reg(0) in (PARENT+0x8630, PARENT+0x8648)
                    assert m.reg(1) == m.reg(2) == 0
                    signals.append(hex(m.reg(0)-PARENT))
                m.ret(irql if name == 'KeGetCurrentIrql' else
                      12345678 if name == 'KeQueryPerformanceCounter' else 0)
            elif pc == BASE+0xdca2b0:
                assert [m.reg(i) for i in (0, 1, 3)] == [OBJECT, BASE+0xe183b0, 0]
                pending.append((m.reg(30), m.reg(4)))
                irql = 3
                stages.append('OS_sync_enter')
                m.u.reg_write(r.UC_ARM64_REG_X0, m.reg(2))
                m.u.reg_write(r.UC_ARM64_REG_X30, STUB+0x200)
                m.u.reg_write(r.UC_ARM64_REG_PC, BASE+0xe183b0)
            elif pc == STUB+0x200:
                resume, flag = pending.pop()
                m.write(flag, 'B', m.reg(0))
                irql = 0
                stages.append('OS_sync_exit')
                m.u.reg_write(r.UC_ARM64_REG_X0, 0)
                m.u.reg_write(r.UC_ARM64_REG_PC, resume)
            elif pc == BASE+0x19e2628:
                assert m.reg(0) == PARENT and m.reg(1) == jobs[0]
                stages.append('reset_reload_boundary')
                m.ret(int(reload_success))
            elif pc == BASE+0xe18220:
                assert [m.reg(i) for i in (0, 1, 2)] == [PARENT, 2, 1]
                stages.append('gate_close' if m.reg(3) else 'gate_open')
            elif pc == BASE+0xe17420:
                stages.append('enqueue_deferred_record')
            elif pc == BASE+0x1a894f0:
                assert m.read(PARENT+0x8678, 'Q') == (0,)
                stages.append('optional_history_absent')

        def write(_, access, address, size, value, user):
            writes.append([hex(address-DESCRIPTOR), size, value,
                           hex(m.u.reg_read(r.UC_ARM64_REG_PC)-BASE)])
        hook_id = m.u.hook_add(UC_HOOK_CODE, hook)
        write_id = m.u.hook_add(UC_HOOK_MEM_WRITE, write, begin=DESCRIPTOR, end=DESCRIPTOR+0xfff)
        result = m.call(0xdd52f0, PARENT, request)
        m.u.hook_del(hook_id)
        m.u.hook_del(write_id)
        assert not pending
        selected = capability and count > 0 and not reject
        succeeded = capability and not (count and reject) and (not count or reload_success)
        expected_status = 0 if succeeded else 0xc00000bb if not capability or reject else 0xc0000001
        assert result == expected_status
        expected_stages = []
        expected_writes = []
        if capability:
            expected_stages = ['gate_close', 'OS_sync_enter', 'OS_sync_exit']
            expected_writes = [['0x844', 4, 0xf00d, '0xe18290']]
            if selected:
                expected_stages += ['optional_history_absent', 'reset_reload_boundary']
            if succeeded:
                expected_stages += ['enqueue_deferred_record']*count
                expected_writes.append(['0x444', 4, count, '0xdd5878'])
            expected_stages.append('gate_open')
            expected_writes.append(['0x844', 4, 0xfeed, '0xe18290'])
        assert stages == expected_stages, stages
        assert writes == expected_writes, writes
        assert signals == ['0x8630', '0x8648']*(2 if capability else 0)
        assert m.read(DESCRIPTOR+0x64, 'I') == (0,)  # Device completion is untouched.
        assert m.read(PARENT+0x137c+4*17, 'I') == (count if selected else 0,)
        assert m.read(PARENT+0x7f08+0x24, 'I') == (count if succeeded else 0,)
        deferred = []
        for i in range(count if succeeded else 0):
            record = m.read(PARENT+0x7f08+0x28+i*0x38, 'QQQQQQQ')
            assert record == (DESCRIPTOR+0x64, i, DESCRIPTOR+0x444, 0, 0, 0, 0)
            deferred.append(dict(completion_offset='0x64', expected_sequence=i,
                                 entry_offset='0x444'))
        worker = None
        if worker_completion is not None:
            assert count == 3 and succeeded
            worker = consume_deferred(m, pe, worker_completion)
        return dict(count=count, reload_success=reload_success, reject_context=reject,
                    capability=capability, status=hex(result), stages=stages,
                    semaphore_writes=writes, event_signals=signals, deferred=deferred,
                    completion_before_worker=0, software_completed=count if selected else 0,
                    worker=worker)
    return replay(pe, count=count, reject=reject, executor=execute)


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[1])
    cases = [run(pe, count=count, reload_success=success)
             for count in (0, 1, 3) for success in (False, True)]
    cases += [run(pe, reject=True), run(pe, capability=False)]
    cases += [run(pe, worker_completion=value) for value in (0, 1, 3, 0xffffffff)]
    print(json.dumps(dict(passed=True, hardware_access=False, kmd_sha256=KMD_SHA256,
        entry='0xdd52f0', cases=cases, limitations=[
            'Constructed adapter/context/job state; capability bit 13 clear and optional history absent',
            'OS locks, IRQL, events, clock and synchronization callback transport modeled',
            'Reset/reload helper 0x19e2628 is a result boundary, not executed here',
            'Worker cases execute one iteration with OS wakeup/memory moves modeled; stop before notification',
            'No physical reset, DMA drain or permission to reclaim memory established']), indent=2))


if __name__ == '__main__':
    main()
