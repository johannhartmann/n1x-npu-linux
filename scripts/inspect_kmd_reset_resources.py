#!/usr/bin/env python3
"""Replay two original DLA timeout-reset resource paths in synthetic RAM.

The RM virtual state callback and allocator release are explicit boundaries.
No MMIO, firmware method, reset or physical memory reclamation is simulated.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import (BASE, OBJECT, OWNER, PRIMARY,
                                         DESCRIPTOR, STUB, ROOT, KMD_SHA256)

RM_ROOT, RM_CONTEXT, RM_DEVICE, RM_VTABLE, ID_POINTER, ID_OBJECT, BRIDGE = (
    OWNER+i*0x1000 for i in range(7))


def state_control(pe, missing=None, active=True, callback_result=0):
    m = DispatchMachine(pe)
    m.write(OBJECT, 'Q', BASE+0xe9c050)
    assert pe.get_data(0xe9c050+0x118, 8) == (BASE+0xdd3800).to_bytes(8, 'little')
    m.write(OBJECT+0xc34, 'B', 0x28 if active else 0x20)
    m.write(OBJECT+0x46b8, 'I', 0x1234)
    m.write(BASE+0x1512148, 'Q', 0 if missing == 'root' else RM_ROOT)
    m.write(RM_ROOT+0x58, 'Q', 0 if missing == 'context' else RM_CONTEXT)
    m.write(RM_ROOT+0x68, 'Q', 0 if missing == 'lookup' else RM_ROOT+0x800)
    m.write(RM_CONTEXT+0x150, 'Q', 0 if missing == 'id_pointer' else ID_POINTER)
    m.write(ID_POINTER, 'Q', 0 if missing == 'id_object' else ID_OBJECT)
    m.write(ID_OBJECT+0x58, 'I', 0x4321 if missing == 'id_mismatch' else 0x1234)
    m.write(RM_CONTEXT+0x178, 'Q', 0 if missing == 'device' else RM_DEVICE)
    m.write(RM_DEVICE, 'Q', RM_VTABLE)
    m.write(RM_VTABLE+0x220, 'Q', STUB+4)
    m.write(RM_DEVICE+0x50, 'I', 1)
    # Original d3e340 -> d4bbd0 writes this optional software state byte.
    m.write(RM_CONTEXT+0x300, 'Q', BRIDGE)
    m.write(BRIDGE+0x60, 'Q', PRIMARY)
    m.write(PRIMARY+0x18, 'Q', PRIMARY+0x1000)
    m.write(PRIMARY+0x1008, 'I', 1)
    m.write(PRIMARY+0x40d8, 'B', 1)
    calls, assertions, lookup_returns = [], [], []

    def hook(_, pc, size, user):
        if pc == STUB+4:
            assert [m.reg(i) for i in range(3)] == [RM_CONTEXT, RM_DEVICE, 0]
            calls.append('rm_virtual_220_zero')
            m.ret(callback_result)
        elif pc == BASE+0xdd1f64:
            lookup_returns.append(m.reg(0))
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc in (BASE+0xce478, BASE+0xce4b0, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            assertions.append(hex(m.reg(30)-BASE))
            m.ret(0)  # Models only a returning assertion handler.

    m.u.hook_add(UC_HOOK_CODE, hook)
    m.call(0xdd3800, OBJECT)
    lookup_ok = active and missing not in (
        'root', 'context', 'lookup', 'id_pointer', 'id_object', 'id_mismatch')
    assert calls == (['rm_virtual_220_zero'] if lookup_ok and missing != 'device' else [])
    assert lookup_returns == ([int(lookup_ok)] if active else [])
    assert m.read(OBJECT+0xc34, 'B') == (0x20,)
    assert m.read(PRIMARY+0x40d8, 'B') == (0 if lookup_ok else 1,)
    assert m.read(RM_DEVICE+0x50, 'I') == (0 if calls else 1,)
    assert bool(assertions) == (active and not lookup_ok)
    return {'active': active, 'missing': missing, 'callbacks': calls,
            'lookup_returns': lookup_returns, 'assertions': assertions,
            'adapter_bit_3_cleared': True,
            'callback_return_register': hex(callback_result)}


def semaphore_release(pe, present, allocated):
    m = DispatchMachine(pe)
    semaphore = PRIMARY+0x1000
    m.write(OBJECT+0x18, 'Q', semaphore if present else 0)
    m.write(semaphore, 'Q', BASE+0xe9cc00)
    assert pe.get_data(0xe9cc18, 8) == (BASE+0x19e38a0).to_bytes(8, 'little')
    m.write(semaphore+0x4e0, 'Q', OWNER)
    m.write(semaphore+0x4e8, 'Q', 0x87654321)
    m.write(semaphore+0x4f0, 'Q', DESCRIPTOR if allocated else 0)
    m.write(semaphore+0x4fc, 'I', 4096)
    # Map the release-manager global before assigning the synthetic owner.
    page = (BASE+0x14bee30+0x3a810) & ~4095
    if not any(a <= page <= b for a, b, _ in m.u.mem_regions()):
        m.u.mem_map(page, 4096)
        m.u.mem_write(page, pe.get_data(page-BASE, 4096))
    m.write(BASE+0x14bee30+0x3a810, 'Q', OWNER+0x8000)
    events = []

    def hook(_, pc, size, user):
        if pc == BASE+0xdc5070:
            assert [m.reg(i) for i in range(5)] == [OWNER+0x8000, OWNER, DESCRIPTOR, 0, 6]
            events.append('allocation_release_kind_6')
            m.ret(0)
        elif pc == BASE+0xe0c8d0:
            assert [m.reg(i) for i in (0, 1)] == [semaphore, 0x734c764e]
            events.append('pool_free')
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    m.call(0x19e3b80, OBJECT)
    assert m.read(OBJECT+0x18, 'Q') == (0,)
    expected = (['allocation_release_kind_6'] if allocated else [])+['pool_free']
    assert events == (expected if present else [])
    if present:
        assert m.read(semaphore+0x4f0, 'Q') == (0,)
        assert m.read(semaphore+0x4fc, 'I') == (0,)
        assert m.read(semaphore+0x4e8, 'Q') == (0 if allocated else 0x87654321,)
    return {'present': present, 'allocated': allocated, 'events': events}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    state = [state_control(pe, missing=x) for x in (
        None, 'root', 'context', 'lookup', 'id_pointer', 'id_object',
        'id_mismatch', 'device')]
    state += [state_control(pe, active=False),
              state_control(pe, callback_result=0xc00000bb)]
    release = [semaphore_release(pe, present, allocated)
               for present in (False, True) for allocated in (False, True)]
    print(json.dumps({'state_control': state, 'semaphore_release': release,
                      'cases': len(state)+len(release),
                      'hardware_quiescence_proven': False}, indent=2))


if __name__ == '__main__':
    main()
