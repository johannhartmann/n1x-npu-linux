#!/usr/bin/env python3
"""Execute Windows deferred job cleanup, with no hardware or OS execution.

The normal walker uses the completed-sequence cache. The lower-level cleanup
routine trusts its caller's limit. OS locks/APC insertion, object getters and
an optional callback are modeled; no buffer destruction is inferred from them.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE

from inspect_kmd_task_submission import (
    Machine, BASE, OBJECT, PARENT, PRIMARY, OWNER, JOB, STUB, ROOT, KMD_SHA256)


def cleanup(pe, *, previous, count, node=0, engine=0, flags=0,
            object_type=(5, 2), busy=False, direct=False):
    m = Machine(pe)
    for page in (0xe04000, 0xe05000, 0xe06000, 0xe07000, 0xe21000, 0xe22000, 0xc000, 0xc8000,
                 0x14be000, 0x14f8000):
        m.u.mem_map(BASE + page, 0x1000)
        m.u.mem_write(BASE + page, pe.get_data(page, 0x1000))
    # Disable diagnostic timing checks and the optional catch-up policy.
    m.write(BASE + 0x14bee24, 'I', 0)
    m.write(BASE + 0x14f8d88, 'B', 0)
    m.write(BASE + 0xe3e000, 'Q', STUB + 4)
    m.write(BASE + 0xe3e298, 'Q', STUB + 8)
    m.write(BASE + 0xe3e2a0, 'Q', STUB + 12)
    m.write(BASE + 0xe3e208, 'Q', STUB + 16)
    m.write(OBJECT, 'Q', OWNER + 0x8000)
    m.write(OBJECT + 0x10, 'II', *object_type)
    m.write(OWNER + 0x8000 + 0x378, 'Q', STUB + 20)
    m.write(OWNER + 0x8000 + 0x260, 'Q', STUB + 24)
    m.write(OWNER + 0x8000 + 0x268, 'Q', STUB + 28)
    m.write(OBJECT + 0x480c, 'B', 1)
    m.write(OBJECT + 0xefc, 'I', engine + 1)
    m.write(PARENT + 0x6ec0, 'Q', OBJECT)
    m.write(OBJECT + 0xed8, 'Q', PRIMARY)
    m.write(PRIMARY + 0x18, 'Q', PRIMARY + 0x1000)
    m.write(PRIMARY + 0x1000 + 0x4f0, 'Q', PRIMARY + 0x2000)
    q = node * 8 + engine
    end = (previous + count) & 0xffffffff
    m.write(PARENT + 0x7d8 + 4*q, 'I', 32)
    m.write(PARENT + 8 + 8*q, 'Q', JOB)
    m.write(PARENT + 0x1b3c + 4*q, 'I', previous)
    # The direct case deliberately supplies a limit ahead of the cache to
    # demonstrate that the low-level helper does not check completion itself.
    m.write(PARENT + 0x137c + 4*q, 'I', previous if direct else end)
    m.write(PARENT + 0xf9c + 4*q, 'I', (end + 1) & 0xffffffff)
    m.write(PARENT + 0x2abc + 4*q, 'I', 7)
    m.write(PARENT + 0x4010 + 4*q, 'I', int(busy))
    records = []
    for ordinal in range(1, count + 2):  # One additional uncompleted record.
        sequence = (previous + ordinal) & 0xffffffff
        record = JOB + (sequence % 32) * 0x1f0
        m.write(record, 'QQQQ', OBJECT, OWNER + 0x3000, OWNER + 0x1000, 0)
        m.write(record + 0x20, 'IIIII', node, engine, ordinal, sequence, 100 + ordinal)
        m.write(record + 0xf8, 'Q', PRIMARY + 0x4000 + ordinal * 16)
        m.write(record + 0x100, 'I', 6)
        m.write(record + 0x118, 'Q', STUB + 32)
        m.write(record + 0x130, 'QQ', STUB + 36, 0x20000 + ordinal)
        m.write(record + 0x1e8, 'I', flags)
        records.append((record, sequence, bytes(m.u.mem_read(record, 0x1f0))))
    events, locks, visited, apcs, delivered = [], [], [], [], []

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            m.ret(2)  # KeGetCurrentIrql: DISPATCH_LEVEL, unlike notification.
        elif pc == STUB + 8:
            assert m.reg(0) == OBJECT + 0x150
            locks.append(('acquire', m.reg(1)))
            m.ret(0)
        elif pc == STUB + 12:
            assert locks[-1] == ('acquire', m.reg(0))
            locks.pop()
            m.ret(0)
        elif pc == STUB + 16:
            record = m.reg(0) - 0x150
            ordinal = next(i+1 for i, (r, _, _) in enumerate(records) if r == record)
            assert [m.reg(i) for i in (1, 2, 3)] == [
                STUB + 36, 0x20000 + ordinal, 0]
            events.append(('queue_apc', ordinal))
            apcs.append((record, m.reg(1), m.reg(2)))
            m.ret(1)  # Enqueued; original trampoline is tested separately below.
        elif pc == STUB + 20:
            assert m.reg(0) == OBJECT
            m.ret(PARENT)
        elif pc == STUB + 24:
            assert m.reg(0) == OBJECT
            m.ret(node + 1)
        elif pc == STUB + 28:
            assert m.reg(0) == OBJECT
            m.write(PRIMARY + 0x3004, 'I', 0 if m.reg(1) == node else 1 << 19)
            m.ret(PRIMARY + 0x3000)
        elif pc == STUB + 32:
            assert m.reg(0) == OBJECT and object_type in ((1, 1), (4, 1))
            events.append(('optional_callback', len(visited)))
            m.ret(0)
        elif pc == STUB + 36:
            record = m.reg(0)
            ordinal = next(i+1 for i, (r, _, _) in enumerate(records) if r == record)
            assert m.reg(1) == 0x20000 + ordinal
            assert m.read(record + 0xf8, 'Q') == (0,)
            delivered.append(ordinal)
            m.ret(0)  # The actual per-job callback is not reconstructed here.
        elif pc == BASE + 0xe21448:
            visited.append(m.reg(0))  # Observe; execute the full original body.
        elif pc == BASE + 0xc520:
            m.write(m.reg(0), 'Q', 1000)
            m.ret(0)
        elif pc in (BASE + 0xe075b0, BASE + 0xce478):
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    if direct:
        m.call(0xe21e20, OBJECT, node, engine, end)
    else:
        result = m.call(0xe04fd0, PARENT)
        assert result == int(count != 0)
    retired = 0 if busy else count
    assert not locks
    assert visited == [r for r, _, _ in records[:retired]]
    assert m.read(PARENT + 0x1b3c + 4*q, 'I') == ((previous + retired) & 0xffffffff,)
    assert m.read(PARENT + 0x137c + 4*q, 'I') == (previous if direct else end,)
    assert m.read(PARENT + 0x4010 + 4*q, 'I') == (int(busy),)
    suppressed = bool(flags & 0x18)
    assert m.read(PARENT + 0x2abc + 4*q, 'I') == (
        100 + retired if retired and not suppressed else 7,)
    expected_events = []
    for ordinal in range(1, retired + 1):
        if not suppressed:
            if object_type in ((1, 1), (4, 1)):
                expected_events.append(('optional_callback', ordinal))
            expected_events.append(('queue_apc', ordinal))
    assert events == expected_events
    for ordinal, (record, _, before) in enumerate(records, 1):
        expected = bytearray(before)
        if ordinal <= retired:
            expected[0xf8:0x100] = bytes(8)
        assert bytes(m.u.mem_read(record, 0x1f0)) == expected
    # Execute the original kernel APC trampoline separately. Its address is
    # installed by the static KeInitializeApc call at 19c47ac; Windows APC
    # scheduling and the initialization function itself are not emulated.
    for record, target, context in apcs:
        m.write(PRIMARY + 0x5000, 'QQ', target, context)
        m.call(0xe063c0, record + 0x150, 0, 0, PRIMARY + 0x5000, PRIMARY + 0x5008)
    assert delivered == [ordinal for event, ordinal in events if event == 'queue_apc']
    return {'retired_records': retired, 'events': events,
            'apc_forwarded_job_ordinals': delivered,
            'pending_record_unchanged': True, 'job_storage_retained': True,
            'completion_cache_checked_by_caller': not direct}


def main():
    path = ROOT / 'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[1])
    imports = {i.address - BASE: i.name for d in pe.DIRECTORY_ENTRY_IMPORT for i in d.imports}
    for offset, name in ((0, b'KeGetCurrentIrql'), (0x298, b'KeAcquireInStackQueuedSpinLock'),
                         (0x2a0, b'KeReleaseInStackQueuedSpinLock'), (0x208, b'KeInsertQueueApc')):
        assert imports[0xe3e000 + offset] == name
    rows = []
    for previous in (0, 31, 0x7fffffff, 0xffffffff):
        for count in (0, 1, 3):
            for node, engine in ((0, 0), (2, 1)):
                for object_type in ((1, 1), (5, 2)):
                    for flags in (0, 8, 16, 24):
                        args = dict(previous=previous, count=count, node=node,
                                    engine=engine, object_type=object_type, flags=flags)
                        rows.append({**args, **cleanup(pe, **args)})
    for direct, busy in ((False, True), (True, False), (True, True)):
        args = dict(previous=31, count=3, direct=direct, busy=busy)
        rows.append({**args, **cleanup(pe, **args)})
    print(json.dumps({'passed': True, 'hardware_access': False,
        'kmd_sha256': KMD_SHA256, 'case_count': len(rows), 'cases': rows,
        'limitations': [
            'Original normal walker, sequence/ring cleanup and selected-record body execute',
            'OS locks, APC insertion, getters, optional callback and logging are synthetic',
            'Original APC trampoline executes separately; OS APC scheduling and actual per-job callback are untested',
            'Nonzero job+148 registry path, graphics and scheduler-specific branches are untested',
            'No allocator, device DMA, reset, real concurrency or physical quiescence is exercised']}, indent=2))


if __name__ == '__main__':
    main()
