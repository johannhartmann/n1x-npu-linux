#!/usr/bin/env python3
"""Replay runtime-generated private data through original KMD job preparation.

The Windows scheduling boundary is modeled by a constructed SubmitCommandVirtual
argument. Original private-header translation, metadata expansion and job record
production execute; final hardware dispatch is observed and not executed.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE

import inspect_windows_submission as umd
from inspect_kmd_task_submission import (
    Machine, BASE, OBJECT, PARENT, PRIMARY, OWNER, DESCRIPTOR, JOB, STUB, ROOT, KMD_SHA256)


def submit(pe, blob, *, last_sequence=0, os_fence=0x87654321, node=0, engine=0,
           cached_replay=False):
    m = Machine(pe)
    for page in (0xdd3000, 0xcd000, 0xe3d000, 0x19b1000, 0xdca000):
        m.u.mem_map(BASE + page, 4096)
        m.u.mem_write(BASE + page, pe.get_data(page, 4096))
    m.write(OBJECT, 'Q', OWNER + 0x8000)
    m.write(OBJECT + 0x10, 'II', 5, 2)
    m.write(OWNER + 0x8000 + 0x378, 'Q', STUB + 4)
    m.write(OWNER + 0x8000 + 0x260, 'Q', STUB + 8)
    m.write(PARENT, 'Q', OWNER + 0x9000)
    m.write(OWNER + 0x9000 + 0x20, 'Q', STUB + 12)
    # Constructed kernel DDI argument, not the user-mode D3DKMT struct.
    m.write(DESCRIPTOR, 'QQ', OWNER + 0x4000, umd.COMMAND)
    m.write(DESCRIPTOR + 0x10, 'I', 0x40)  # Same modeled command length as UMD replay.
    m.write(DESCRIPTOR + 0x18, 'Q', PRIMARY)
    m.write(DESCRIPTOR + 0x20, 'III', 0x590, len(blob), os_fence)
    m.u.mem_write(PRIMARY, blob + b'\xa5' * (0x590 - len(blob)))
    m.write(OWNER + 0x4000, 'Q', OWNER + 0x5000)
    m.write(OWNER + 0x4000 + 0x70, 'I', 2)
    m.write(OWNER + 0x4000 + 0x78, 'Q', OWNER + 0x6000)
    m.write(OWNER + 0x4000 + 0x80, 'I', node)
    m.write(OWNER + 0x4000 + 0x8c, 'I', 2)
    m.write(OWNER + 0x4000 + 0x248, 'I', 19)
    m.write(OWNER + 0x5000 + 0x38, 'Q', STUB + 16)
    m.write(OWNER + 0x5000 + 0x80, 'Q', STUB + 28)
    m.write(OWNER + 0x6000, 'Q', OWNER + 0xa000)
    m.write(OWNER + 0xa000 + 0xa0, 'Q', STUB + 32)
    m.write(OWNER + 0x6000 + 0xd8, 'I', 40)
    m.write(OBJECT + 0xee0, 'Q', OWNER + 0x7000)
    m.write(OWNER + 0x7000 + 0x10, 'I', 4)
    m.write(OWNER + 0x7000 + node * 0x170 + 0x18, 'I', 0x33)
    m.write(BASE + 0xe3e0c8, 'Q', STUB + 20)
    m.write(BASE + 0xe3e0d0, 'Q', STUB + 24)
    q = node * 8 + engine
    m.write(PARENT + 0x7d0, 'I', 32)
    m.write(PARENT + 0x7d8 + q*4, 'I', 32)
    m.write(PARENT + 0xf98, 'I', 0x5678)
    m.write(PARENT + 0xf9c + q*4, 'I', last_sequence)
    m.write(PARENT + (q+1)*8, 'Q', JOB)
    m.u.mem_write(JOB, b'\xa5' * (32 * 0x1f0))
    sequence = (last_sequence + 1) & 0xffffffff
    record = JOB + (sequence % 32) * 0x1f0
    observed, helpers = [], []

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            assert m.reg(0) == OBJECT
            m.ret(PARENT)
        elif pc == STUB + 8:
            assert m.reg(0) == OBJECT
            m.ret(4)
        elif pc == STUB + 12:
            assert m.reg(0) == PARENT and m.reg(2) == record
            observed.append(bytes(m.u.mem_read(record, 0x1f0)))
            m.ret(0)  # Final dispatch boundary; no task is sent to firmware.
        elif pc == STUB + 16:
            assert m.reg(0) == OWNER + 0x4000
            m.ret(engine)
        elif pc == STUB + 20:
            assert m.reg(0) == OBJECT + 0x58 + node*8
            m.ret(2)
        elif pc == STUB + 24:
            assert [m.reg(i) for i in (0, 1)] == [OBJECT + 0x58 + node*8, 2]
            m.ret(0)
        elif pc == STUB + 28:
            assert m.reg(0) == OWNER + 0x4000
            m.ret(0)
        elif pc == STUB + 32:
            assert m.reg(0) == OWNER + 0x6000
            m.ret(OWNER + 0xb000)
        elif pc == BASE + 0xcd148:
            assert m.reg(1) == 0 and m.reg(2) == 0x590
            m.u.mem_write(m.reg(0), bytes(m.reg(2)))
            helpers.append('zero_expanded_metadata')
            m.ret(m.reg(0))
        elif pc == BASE + 0xe3d030:
            assert m.reg(0) == PRIMARY and m.reg(2) == 0x590
            m.u.mem_write(m.reg(0), bytes(m.u.mem_read(m.reg(1), m.reg(2))))
            helpers.append('copy_expanded_metadata')
            m.ret(m.reg(0))

    m.u.hook_add(UC_HOOK_CODE, hook)
    status = m.call(0x19b1500, 0, OBJECT, DESCRIPTOR)
    assert status == 0 and len(observed) == 1
    assert helpers == ['zero_expanded_metadata', 'copy_expanded_metadata']
    data = observed[0]
    assert struct.unpack_from('<QQQ', data) == (OBJECT, OWNER + 0x6000, OWNER + 0x4000)
    assert struct.unpack_from('<IIIII', data, 0x20) == (node, engine, 0x5679, sequence, os_fence)
    assert struct.unpack_from('<Q', data, 0x38) == (umd.COMMAND,)
    assert struct.unpack_from('<I', data, 0x34) == (0x40,)
    assert struct.unpack_from('<Q', data, 0xf8) == (PRIMARY,)
    assert struct.unpack_from('<I', data, 0x100) == (1,)
    assert data[0x108:0x1b0] == bytes(0xa8)
    assert struct.unpack_from('<II', data, 0x1e8) == (0, 0)
    assert m.read(PRIMARY + 0x140, 'QQ') == (OBJECT, OWNER + 0x6000)
    assert m.read(PRIMARY + 0x150, 'QQQQQQQ') == (0,) * 7
    assert m.read(PRIMARY + 0x53c, 'B') == (1,)
    assert m.read(PRIMARY + 0x540, 'I') == (0x4e564441,)
    assert m.read(PRIMARY + 0x354, 'I') == (41,)
    assert m.read(OWNER + 0x4000 + 0x248, 'I') == (20,)
    assert m.read(OWNER + 0x6000 + 0xd8, 'I') == (41,)
    replay = None
    if cached_replay:
        # Re-enter with the translated private record and its cache marker.
        # This tests a KMD path, not permission for the Linux API to retry jobs.
        private_before = bytes(m.u.mem_read(PRIMARY, 0x590))
        next_sequence = (sequence + 1) & 0xffffffff
        next_fence = (os_fence + 1) & 0xffffffff
        record = JOB + (next_sequence % 32) * 0x1f0
        observed.clear()
        helpers.clear()
        m.write(DESCRIPTOR + 0x28, 'I', next_fence)
        assert m.call(0x19b1500, 0, OBJECT, DESCRIPTOR) == 0
        assert len(observed) == 1 and not helpers
        assert bytes(m.u.mem_read(PRIMARY, 0x590)) == private_before
        assert struct.unpack_from('<II', observed[0], 0x2c) == (next_sequence, next_fence)
        assert observed[0][0x108:0x1b0] == bytes(0xa8)
        assert m.read(OWNER + 0x4000 + 0x248, 'I') == (20,)
        assert m.read(OWNER + 0x6000 + 0xd8, 'I') == (41,)
        replay = {'sequence': next_sequence, 'os_fence': next_fence,
                  'metadata_unchanged': True, 'new_job_created': True}
    return {'sequence': sequence, 'os_fence': os_fence, 'node': node, 'engine': engine,
            'job_type': 1, 'callbacks_and_apc_zero': True,
            'kernel_private_bytes': 0x590, 'final_dispatch_modeled': True,
            'cached_replay': replay}


def main():
    runtime = umd.image('nvdla_runtime.dll', umd.RUNTIME_HASH, umd.BASE)
    cudla = umd.image('nvcudla.dll', umd.CUDLA_HASH, umd.CBASE)
    producer = umd.Machine(runtime, cudla)
    assert producer.call(umd.BASE + 0x75858, umd.CTX, umd.TASK, 1, 7) == 0
    blob = bytes(producer.u.mem_read(umd.PRIVATE, 0x354))
    assert blob == struct.pack('<QII', 0x100044e564441, 0, 0x354) + bytes(0x344)
    path = ROOT / 'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for previous in (0, 31, 0x7fffffff, 0xffffffff):
        for os_fence in (0, 0x87654321):
            for node, engine in ((0, 0), (2, 1)):
                for cached_replay in (False, True):
                    rows.append(submit(pe, blob, last_sequence=previous,
                                       os_fence=os_fence, node=node, engine=engine,
                                       cached_replay=cached_replay))
    print(json.dumps({'passed': True, 'hardware_access': False, 'case_count': len(rows),
        'kmd_sha256': KMD_SHA256, 'runtime_sha256': umd.RUNTIME_HASH,
        'cudla_sha256': umd.CUDLA_HASH, 'input_private_sha256': hashlib.sha256(blob).hexdigest(),
        'cases': rows, 'limitations': [
            'Runtime command builder, OS scheduling transition, object getters and locks are modeled',
            'Kernel metadata allocation capacity is constructed; no OS allocation negotiation executes',
            'Original class dispatcher 19b1500, dd3130, dd6188 and dd28f8 execute; memset/memcpy use exact byte models',
            'Final hardware dispatch and physical memory lifetime are not executed']}, indent=2))


if __name__ == '__main__':
    main()
