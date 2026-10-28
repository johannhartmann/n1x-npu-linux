#!/usr/bin/env python3
"""Replay the Windows outstanding-job bookkeeping helper, without hardware.

This is not a reset implementation. It isolates the helper reached from the
DLA adapter's slot-0x40 path after its gate/OS synchronization boundaries.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

from inspect_kmd_task_submission import (Machine, BASE, OBJECT, PARENT, OWNER,
                                         PRIMARY, DESCRIPTOR, JOB, STUB, ROOT,
                                         KMD_SHA256)

ARGS, REQUEST, OUTPUT, CONTEXT = (PRIMARY+i*0x1000 for i in range(1, 5))


def replay(pe, previous=0, count=1, reject=False, flags=0):
    m = Machine(pe)
    for page in (0xe18000, 0xe19000, 0xe07000, 0xc000, 0x5000, 0x92000, 0xe9c000,
                 0xdf8000, 0x14be000, 0x14f8000):
        m.u.mem_map(BASE+page, 0x1000)
        m.u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    assert pe.get_data(0xe9c950, 8) == (BASE+0xdd52f0).to_bytes(8, 'little')
    m.write(OBJECT, 'Q', OWNER+0x8000)
    m.write(OBJECT+0x10, 'II', 5, 2)
    m.write(OWNER+0x8000+0x378, 'Q', STUB+4)
    m.write(BASE+0xe3e000, 'Q', STUB+8)
    m.write(PARENT, 'Q', OWNER+0x9000)
    # These two concrete adapter vtable entries return true/false unchanged.
    assert pe.get_data(0xe9c9f8, 8) == (BASE+0x5bf0).to_bytes(8, 'little')
    assert pe.get_data(0xe9ca00, 8) == (BASE+0x5be0).to_bytes(8, 'little')
    m.write(OWNER+0x9000+0xe8, 'Q', BASE+0x5bf0)
    m.write(OWNER+0x9000+0xf0, 'Q', BASE+0x5be0)
    m.write(OWNER+0x9000+0xd0, 'Q', BASE+0xdd6440)
    m.write(PARENT+0x8680, 'Q', OWNER+0x6000)
    m.write(OWNER+0x6000, 'Q', OWNER+0x7000)
    m.write(OWNER+0x7000+0x18, 'Q', STUB+16)
    # The original DLA context factory installs this table. Slot 0x98 is
    # an atomic counter increment, not an engine-stop operation; its complete
    # instructions now execute here rather than an injected callback return.
    m.write(CONTEXT, 'Q', BASE+0xe9c628)
    m.write(CONTEXT+0x70, 'I', 2)
    assert m.read(BASE+0xe9c628+0x98, 'Q') == (BASE+0x92c30,)
    m.write(CONTEXT+0x8c, 'I', 0 if reject else 2)
    for address, pointer in ((OBJECT+0xed8, OWNER), (OWNER+0x18, OWNER+0x1000),
                             (OWNER+0x1000+0x4e0, OWNER+0x2000),
                             (OWNER+0x1000+0x4f0, DESCRIPTOR)):
        m.write(address, 'Q', pointer)
    m.write(OWNER+0x2000+0xefc, 'I', 8)
    m.write(OWNER+0x2000+0xf00, 'I', 4)
    m.write(OWNER+0x1000+0x52c, 'III', 0x100, 256, 4)
    m.write(OWNER+0x1000+0x508, 'III', 0x10, 256, 4)
    node, engine = 2, 1
    q = node*8+engine
    completed = (previous+count)&0xffffffff
    entry = (previous+1)&0xffffffff if count else previous
    entry_offset = (0x100+8*node+engine)*4
    completion_offset = (0x10+4*node+engine)*4
    m.write(DESCRIPTOR+entry_offset, 'I', entry)
    # A distinct, untouched completion-bank sentinel. Only the entry bank is
    # read by the tested helper; it does not observe a new hardware completion.
    m.write(DESCRIPTOR+completion_offset, 'I', previous)
    for offset, value in ((0x7d8, 32), (0x137c, previous), (0xf9c, completed),
                          (0x1b3c, previous), (0x26dc, 0x12340000),
                          (0x2abc, 0x12340000), (0x384c, 0x5566),
                          (0x3c2c, 0x7788), (0xbb8, count)):
        m.write(PARENT+offset+4*q, 'I', value)
    m.write(PARENT+0x3754+q, 'B', 1)
    m.write(PARENT+8+8*q, 'Q', JOB)
    jobs = []
    for i in range(1, count+1):
        seq = (previous+i)&0xffffffff
        address = JOB+(seq%32)*0x1f0
        jobs.append(address)
        m.write(address+0x10, 'Q', CONTEXT)
        m.write(address+0x2c, 'II', seq, 0x12340000+i)
        m.write(address+0x100, 'I', 1)
        m.write(address+0x1b0, 'Q', 100+i)
        m.write(address+0x1e8, 'I', flags)
    m.write(ARGS, 'QQ', OBJECT, REQUEST)
    m.write(ARGS+0x20, 'Q', OUTPUT)
    m.write(ARGS+0x28, 'I', 32)
    m.write(ARGS+0x30, 'I', 0xc0000001)
    m.write(REQUEST, 'III', node, engine, 0xdeadbeef)
    events, bank_reads, bank_writes = [], [], []

    def hook(_, pc, size, user):
        if pc == STUB+4:
            assert m.reg(0) == OBJECT
            m.ret(PARENT)
        elif pc == STUB+8:
            m.ret(3)  # The original helper asserts IRQL > DISPATCH_LEVEL.
        elif pc == BASE+0x92c30:
            assert [m.reg(i) for i in (0, 1)] == [CONTEXT, 0]
            events.append('context_slot_98')
        elif pc == STUB+16:
            assert m.reg(0) == OWNER+0x6000 and m.reg(1) in jobs[1:]
            events.append('worker_slot_18')
            m.ret(0)
        elif pc in (BASE+0xce478, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            raise AssertionError(('unexpected vendor assertion', hex(m.reg(30)-BASE), previous, count))

    def read(_, access, address, size, value, user):
        bank_reads.append((address-DESCRIPTOR, size))

    def write(_, access, address, size, value, user):
        bank_writes.append((address-DESCRIPTOR, size, value))

    m.u.hook_add(UC_HOOK_CODE, hook)
    m.u.hook_add(UC_HOOK_MEM_READ, read, begin=DESCRIPTOR, end=DESCRIPTOR+0xfff)
    m.u.hook_add(UC_HOOK_MEM_WRITE, write, begin=DESCRIPTOR, end=DESCRIPTOR+0xfff)
    result = m.call(0xe183b0, ARGS)
    status, = m.read(ARGS+0x30, 'I')
    assert not bank_writes
    assert m.read(DESCRIPTOR+entry_offset, 'I') == (entry,)
    assert m.read(DESCRIPTOR+completion_offset, 'I') == (previous,)
    assert m.read(CONTEXT+0x6c, 'I') == (int(bool(count) and not reject),)
    if reject and count:
        assert result == 0 and status == 0xc00000bb
        assert not events and not bank_reads
        assert m.read(PARENT+0x137c+4*q, 'I') == (previous,)
        assert m.read(PARENT+0x1b3c+4*q, 'I') == (previous,)
        assert m.read(ARGS+0x2c, 'I') == (0,)
        assert m.read(REQUEST+8, 'I') == (0xdeadbeef,)
    else:
        assert result == 1 and status == 0
        assert m.read(PARENT+0x137c+4*q, 'I') == (completed,)
        assert m.read(PARENT+0x1b3c+4*q, 'I') == (completed,)
        assert m.read(PARENT+0x3c2c+4*q, 'I') == (0x5566,)
        assert m.read(PARENT+0xbb8+4*q, 'I') == (0,)
        assert m.read(PARENT+0x3754+q, 'B') == (0,)
        assert m.read(ARGS+0x2c, 'I') == (count,)
        for i, address in enumerate(jobs):
            assert m.read(OUTPUT+4*i, 'I') == ((previous+i+1)&0xffffffff,)
            assert m.read(address+0x1e8, 'I') == (flags|8,)
        expected_fence = 0x12340000+(0 if not count or flags&4 and not flags&512 else 1)
        assert m.read(REQUEST+8, 'I') == (expected_fence,)
        assert bank_reads == ([(entry_offset, 4)] if count else [])
        assert events == (['context_slot_98']+['worker_slot_18']*(count-1) if count else [])
    return {'previous': previous, 'outstanding_count': count,
            'context_mode': 0 if reject else 2, 'job_flags': flags,
            'return': result, 'status': hex(status),
            'software_completed': m.read(PARENT+0x137c+4*q, 'I')[0],
            'device_bank_unchanged': True, 'bank_reads': bank_reads,
            'original_context_counter': m.read(CONTEXT+0x6c, 'I')[0],
            'events': events}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = []
    for previous in (0, 31, 0x7fffffff, 0xffffffff):
        for count in (0, 1, 3):
            cases.append(replay(pe, previous, count))
    for flags in (4, 512, 516):
        cases.append(replay(pe, flags=flags))
    cases.append(replay(pe, reject=True))
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'kmd_sha256': KMD_SHA256, 'helper_rva': '0xe183b0',
                      'cases': cases, 'limitations': [
                          'Constructed queues, contexts and mapped semaphore banks',
                          'Context slot 0x98 executes original 0x92c30; worker callback, IRQL, timestamps and logging modeled',
                          'Caller gate, OS synchronization and surrounding recovery not executed',
                          'No reset, MMIO, engine stop, firmware execution or DMA release',
                          'Software sequence advancement is not physical task-completion evidence']}, indent=2))


if __name__ == '__main__':
    main()
