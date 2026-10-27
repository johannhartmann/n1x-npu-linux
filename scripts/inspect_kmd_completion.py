#!/usr/bin/env python3
"""Execute original Windows job-completion notification, without hardware.

Tests both the selected-job callback and the original completion-bank reader
which selects ring records. CPU-mapped device memory is synthetic; this is
not a physical IRQ, DMA-coherency or whole-device quiescence test.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE

from inspect_kmd_task_submission import (
    Machine, BASE, OBJECT, PARENT, OWNER, JOB, DESCRIPTOR, PRIMARY, STUB, ROOT,
    KMD_SHA256, postaction, run_postactions, HOST, PROFILE1_HASH)


def complete(pe, *, sequence, os_fence, flags=0, node=0, engine=0, callback=False,
             selection=None, observed_word=None, object_type=(1, 1), job_type=6):
    m = Machine(pe)
    for page in (0xe21000, 0xe22000, 0xe05000, 0xe07000, 0xdeb000, 0xc000,
                 0x14be000, 0x14c1000):
        m.u.mem_map(BASE + page, 0x1000)
        m.u.mem_write(BASE + page, pe.get_data(page, 0x1000))
    # Global timing-policy switch; exercise normal accounting, not its bypass.
    m.write(BASE + 0x14c1570, 'I', 0)
    m.write(BASE + 0xe3e000, 'Q', STUB + 4)
    m.write(BASE + 0xe3e008, 'Q', STUB + 8)
    m.write(OBJECT, 'Q', OWNER + 0x8000)
    m.write(OBJECT + 0x10, 'II', *object_type)
    m.write(OWNER + 0x8000 + 0x378, 'Q', STUB + 12)
    m.write(OWNER + 0x8000 + 0x290, 'Q', STUB + 16)
    m.write(OBJECT + 0x1988, 'Q', OWNER + 0x6000)
    m.write(OWNER + 0x6000, 'Q', OWNER + 0x7000)
    m.write(OWNER + 0x7000 + 0x18, 'Q', STUB + 20)
    m.write(OWNER + 0x5000 + 8, 'Q', 0x87654321)
    m.write(OWNER + 0x5000 + 0x80, 'Q', STUB + 24)
    m.write(JOB, 'QQQQ', OBJECT, OWNER + 0x3000, OWNER + 0x1000, OWNER + 0x2000)
    m.write(JOB + 0x20, 'IIIII', node, engine, 0x1234, sequence, os_fence)
    m.write(JOB + 0x100, 'I', job_type)
    m.write(JOB + 0x1b0, 'Q', 900)
    m.write(JOB + 0x1e8, 'I', flags)
    m.write(OWNER + 0x2000 + 0x118, 'I', 1)
    if callback:
        m.write(JOB + 0x108, 'QQ', STUB + 28, 0x13579)
    queue = node * 8 + engine
    m.write(PARENT + queue * 4 + 0xbb8, 'I', 1)
    m.write(PARENT + queue * 4 + 0x26dc, 'I', 7)
    job = JOB
    previous = (sequence - 1) & 0xffffffff
    selected = selection is None or selection in ('ready', 'fallback')
    if selection is not None:
        assert selection in ('ready', 'pending', 'stale', 'too_far', 'blocked',
                             'missing', 'fallback')
        # Original e22500 -> dd1548 reads F+4f0; dd15b0 -> dd7708
        # selects completion bank F+508, with Q.f00*node+engine.
        m.write(OBJECT + 0xed8, 'Q', PRIMARY)
        m.write(PRIMARY + 0x18, 'Q', PRIMARY + 0x1000)
        m.write(PRIMARY + 0x1000 + 0x4e0, 'Q', PRIMARY + 0x2000)
        m.write(PRIMARY + 0x1000 + 0x4f0, 'Q',
                0 if selection == 'missing' else DESCRIPTOR)
        m.write(PRIMARY + 0x2000 + 0xf00, 'I', 4)
        m.write(PRIMARY + 0x1000 + 0x508, 'III', 0x40, 32, 16)
        index = 0x40 + 4 * (4 * node + engine)
        observed = {'pending': previous, 'fallback': previous,
                    'stale': (previous - 1) & 0xffffffff,
                    'too_far': (previous + 32) & 0xffffffff}.get(selection, sequence)
        if observed_word is not None:
            assert observed_word == observed
            observed = observed_word
        m.write(DESCRIPTOR + 4 * index, 'I', observed)
        # Wrong bank and wrong stride/multiplier offsets remain zero.
        m.write(OBJECT + 0x480c, 'B', selection != 'fallback')
        m.write(OBJECT + 0x1c8, 'Q', PRIMARY + 0x3000)
        # Invalid deltas take a non-recovery branch under this explicit policy.
        m.write(PRIMARY + 0x3000 + 4, 'I', 1 << 13)
        m.write(PARENT + queue * 4 + 0xf9c, 'I', sequence)
        m.write(PARENT + queue * 4 + 0x137c, 'I', previous)
        m.write(PARENT + queue * 4 + 0x7d8, 'I', 32)
        m.write(PARENT + 8 + queue * 8, 'Q', JOB)
        job = JOB + (sequence % 32) * 0x1f0
        template = bytes(m.u.mem_read(JOB, 0x1f0))
        m.u.mem_write(job, template)
        if selection == 'blocked':
            m.write(job + 0x1e8, 'I', flags | (1 << 10))
        m.write(PRIMARY + 0x4000, 'QII', OBJECT, node, engine)
    events = []

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            m.ret(3)  # KeGetCurrentIrql: constructed interrupt-level context.
        elif pc == STUB + 8:
            if m.reg(0):
                m.write(m.reg(0), 'Q', 10000000)
            m.ret(1000)  # KeQueryPerformanceCounter.
        elif pc == STUB + 12:
            assert m.reg(0) == OBJECT
            m.ret(PARENT)
        elif pc == STUB + 16:
            assert [m.reg(i) for i in (0, 1)] == [OBJECT, node]
            m.ret(0)  # Optional engine notification branch disabled.
        elif pc == STUB + 20:
            assert m.reg(0) == OWNER + 0x6000
            m.ret(OWNER + 0x5000)  # Constructed DXGKRNL_INTERFACE.
        elif pc == STUB + 24:
            assert m.reg(0) == 0x87654321
            event = m.reg(1)
            assert m.read(event, 'I') == (1,)  # DXGK_INTERRUPT_DMA_COMPLETED.
            assert m.read(event + 8, 'III') == (os_fence, node, engine)
            assert m.read(event + 0x48, 'I') == (0,)
            assert m.read(PARENT + queue * 4 + 0x26dc, 'I') == (os_fence,)
            assert m.read(PARENT + queue * 4 + 0x1f1c, 'I') == (sequence,)
            assert m.read(OBJECT + 0x4b48, 'I') == (0,)
            events.append({'event': 'notify_interrupt', 'type': 1,
                           'os_fence': os_fence, 'node': node, 'engine': engine})
            m.ret(0)
        elif pc == STUB + 28:
            assert [m.reg(i) for i in (0, 1)] == [job, 0x13579]
            assert m.read(PARENT + queue * 4 + 0x26dc, 'I') == (7,)
            events.append({'event': 'job_callback_before_os_notification'})
            m.ret(0)
        elif pc == BASE + 0xc520:
            m.write(m.reg(0), 'Q', 1000)
            m.ret(0)
        elif pc in (BASE + 0xe075b0, BASE + 0xce478):
            m.ret(0)
        elif pc == BASE + 0xdeb120:
            m.ret(7)  # Timestamp conversion is outside the completion contract.

    m.u.hook_add(UC_HOOK_CODE, hook)
    if selection is None:
        m.call(0xe218b8, job)
    else:
        result = m.call(0xe22500, PRIMARY + 0x4000)
        assert result == int(selection in ('ready', 'fallback', 'blocked'))
        assert m.read(PARENT + queue * 4 + 0x137c, 'I') == (
            sequence if selected else previous,)
    assert m.read(PARENT + queue * 4 + 0xbb8, 'I') == (0 if selected else 1,)
    assert m.read(OWNER + 0x2000 + 0x118, 'I') == (0 if selected else 1,)
    assert m.read(job + 0x1b8, 'Q') == (1000 if selected else 0,)
    assert m.read(job + 0x2c, 'II') == (sequence, os_fence)
    notified = selected and not (flags & 0x18)
    assert m.read(OBJECT + 0x4b48, 'I') == (int(notified),)
    assert m.read(PARENT + queue * 4 + 0x26dc, 'I') == (os_fence if notified else 7,)
    assert m.read(PARENT + queue * 4 + 0x1f1c, 'I') == (sequence if notified else 0,)
    expected = []
    if notified:
        if callback:
            expected.append({'event': 'job_callback_before_os_notification'})
        expected.append({'event': 'notify_interrupt', 'type': 1,
                         'os_fence': os_fence, 'node': node, 'engine': engine})
    assert events == expected
    return {'selected': selected, 'notified': notified, 'events': events,
            'outstanding_after': int(not selected),
            'job_record_still_present': True}


def main():
    path = ROOT / 'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
    imports = {i.address - BASE: i.name for dll in pe.DIRECTORY_ENTRY_IMPORT for i in dll.imports}
    assert imports[0xe3e000] == b'KeGetCurrentIrql'
    assert imports[0xe3e008] == b'KeQueryPerformanceCounter'
    rows = []
    for os_fence in (8, 0x80000000, 0xffffffff):
        for node, engine in ((0, 0), (2, 1)):
            for flags in (0, 8, 16, 24):
                for callback in (False, True):
                    args = dict(sequence=32, os_fence=os_fence, node=node, engine=engine,
                                flags=flags, callback=callback)
                    rows.append({**args, **complete(pe, **args)})
    for sequence in (0, 1, 32, 0x80000000, 0xffffffff):
        for node, engine in ((0, 0), (2, 1)):
            for selection in ('ready', 'pending', 'stale', 'too_far', 'blocked',
                              'missing', 'fallback'):
                args = dict(sequence=sequence, os_fence=0x12345678, node=node,
                            engine=engine, callback=True, selection=selection)
                rows.append({**args, **complete(pe, **args)})
    # The DLA-specific dd5ab0 dispatcher checks object class (5,2), while
    # some optional graphics branches require (1,1)/(4,1). Do not establish
    # the common completion contract solely with the latter constructed type.
    for job_type in (1, 6):
        for flags in (0, 8, 16, 24):
            for selection in (None, 'ready', 'pending', 'blocked'):
                args = dict(sequence=32, os_fence=0x12345678, flags=flags,
                            selection=selection, object_type=(5, 2), job_type=job_type)
                rows.append({**args, **complete(pe, **args)})
    firmware = (ROOT / 'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    for sequence in (0, 1, 0x80000000, 0xffffffff):
        for node in (0, 2):
            descriptor, stream, offset = postaction(pe, index=node, multiplier=4,
                base_index=0x40, stride=16, value=sequence)
            descriptor = bytearray(descriptor)
            descriptor[15] = 1  # Count set by dd75ac, outside the tested builder.
            for task_status in (0, -1):
                fw = run_postactions(firmware, stream, offset=offset,
                    descriptor=descriptor, task_status=task_status,
                    initial_words={HOST: (sequence - 1) & 0xffffffff})
                assert fw['returned'] and fw['status'] == 0
                assert [t['direction'] for t in fw['transfers']] == (
                    ['read', 'read', 'write'] if task_status == 0 else [])
                args = dict(sequence=sequence, os_fence=0x12345678, node=node,
                    selection='ready' if task_status == 0 else 'pending',
                    observed_word=fw['host_word'])
                rows.append({'case': 'windows_producer_firmware_consumer_windows_completion',
                    'task_status': task_status, 'firmware': fw,
                    **args, **complete(pe, **args)})
    print(json.dumps({'passed': True, 'hardware_access': False, 'case_count': len(rows),
        'kmd_sha256': KMD_SHA256, 'firmware_sha256': PROFILE1_HASH, 'cases': rows,
        'limitations': [
            'Selected callback e218b8 and completion-bank/ring reader e22500 execute; device memory is synthetic',
            'Invalid completion deltas use capability bit 13 to skip recovery; recovery branches are untested',
            'OS callbacks, adapter getters, timing and logging are synthetic',
            'Optional engine notification and graphics-specific job branches are disabled',
            'Original e05cf8 wrapper and e07010 metadata extractor execute',
            'Firmware and KMD run separately with an explicit copy of the completion word; RAM-only DMA, no engine arithmetic',
            'No physical completion, scheduler acknowledgment, allocation destruction or DMA quiescence is proven']}, indent=2))


if __name__ == '__main__':
    main()
