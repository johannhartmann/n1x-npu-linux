#!/usr/bin/env python3
"""Replay two original DLA timeout-reset resource paths in synthetic RAM.

The initial cases inject the RM virtual callback; concrete-constructor cases
execute it through the original interrupt-mask accessors. Register I/O and
allocator release are explicit boundaries. No physical hardware is accessed.
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


def state_control(pe, missing=None, active=True, callback_result=0,
                  concrete=None, current=0xffffffff, owned=0xc0,
                  read_error=0, write_error=0):
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
    device = RM_DEVICE
    saved_offset = None
    if concrete is not None:
        assert missing is None and active and concrete in (0x71, 0x72)
        ctor, selector, saved_offset, target = (
            (0x195d038, 0x404044, 0x49c, 0xd49800) if concrete == 0x71 else
            (0x1964a48, 0x50200, 0x2c8, 0xd4d9f0))
        m.write(RM_VTABLE, 'Q', RM_DEVICE)
        # Original constructor uses caller-provided storage (bit 1), with no
        # parent registration (bit 0). Its helpers and HAL selector execute.
        assert m.call(ctor, RM_VTABLE, 0, 3, selector, selector) == 0
        device, = m.read(RM_DEVICE+0x68, 'Q')
        assert device == RM_DEVICE+8
        m.write(device+0x54, 'I', owned)
        m.write(RM_DEVICE+saved_offset, 'I', 0x12340000)
    else:
        m.write(RM_DEVICE, 'Q', RM_VTABLE)
        m.write(RM_VTABLE+0x220, 'Q', STUB+4)
    m.write(RM_CONTEXT+0x178, 'Q', 0 if missing == 'device' else device)
    m.write(device+0x50, 'I', 1)
    # Original d3e340 -> d4bbd0 writes this optional software state byte.
    m.write(RM_CONTEXT+0x300, 'Q', BRIDGE)
    m.write(BRIDGE+0x60, 'Q', PRIMARY)
    m.write(PRIMARY+0x18, 'Q', PRIMARY+0x1000)
    m.write(PRIMARY+0x1008, 'I', 1)
    m.write(PRIMARY+0x40d8, 'B', 1)
    calls, assertions, lookup_returns, io = [], [], [], []

    def hook(_, pc, size, user):
        if pc == STUB+4:
            assert [m.reg(i) for i in range(3)] == [RM_CONTEXT, RM_DEVICE, 0]
            calls.append('rm_virtual_220_zero')
            m.ret(callback_result)
        elif concrete is not None and pc == BASE+target:
            assert [m.reg(i) for i in range(3)] == [RM_CONTEXT, RM_DEVICE, 0]
            calls.append('rm_virtual_220_zero')
            # Execute the original callback, subobject thunks and accessors.
        elif concrete is not None and pc == BASE+0xd39878:
            assert m.reg(0) == RM_CONTEXT
            assert m.reg(1) == (0 if concrete == 0x71 else 1)
            offset = m.reg(2)
            assert offset in (0xd28, 0xd2c)
            value = current if offset == 0xd28 else owned
            io.append(['read', hex(offset), value, read_error])
            if not read_error:
                m.write(m.reg(3), 'I', value)
            m.ret(read_error)
        elif concrete is not None and pc == BASE+0xd39f38:
            assert [m.reg(i) for i in (0, 1, 2)] == [
                RM_CONTEXT, 0 if concrete == 0x71 else 1, 0xd24]
            io.append(['write', '0xd24', m.reg(3), write_error])
            m.ret(write_error)
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
    assert m.read(device+0x50, 'I') == (0 if calls else 1,)
    assert bool(assertions) == (active and not lookup_ok)
    if concrete is not None:
        expected_io = [['read', '0xd28', current, read_error]]
        if not read_error:
            if concrete == 0x72:
                expected_io.append(['read', '0xd2c', owned, 0])
            expected_io.append(['write', '0xd24', current & owned, write_error])
        assert io == expected_io
        expected_saved = current & owned if not read_error and current & owned else 0x12340000
        assert m.read(RM_DEVICE+saved_offset, 'I') == (expected_saved,)
    return {'active': active, 'missing': missing, 'callbacks': calls,
            'lookup_returns': lookup_returns, 'assertions': assertions,
            'adapter_bit_3_cleared': True,
            'callback_return_register': hex(callback_result) if concrete is None else None,
            'concrete_class': hex(concrete) if concrete else None, 'register_io': io}


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
    for concrete in (0x71, 0x72):
        for current, owned in ((0xffffffff, 0xc0), (0x12345678, 0xf0), (0, 0xc0)):
            for read_error, write_error in ((0, 0), (0x65, 0), (0, 0x65)):
                state.append(state_control(pe, concrete=concrete, current=current,
                    owned=owned, read_error=read_error, write_error=write_error))
    release = [semaphore_release(pe, present, allocated)
               for present in (False, True) for allocated in (False, True)]
    print(json.dumps({'state_control': state, 'semaphore_release': release,
                      'cases': len(state)+len(release),
                      'hardware_quiescence_proven': False}, indent=2))


if __name__ == '__main__':
    main()
