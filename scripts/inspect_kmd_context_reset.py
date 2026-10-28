#!/usr/bin/env python3
"""Resolve the DLA context reset callback via the original creation prefix.

Stops immediately after the concrete context vtable is installed. Pool memory,
OS primitive initialization, timestamps and logging are modeled. No complete
context creation, Windows device or physical reset is claimed.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import arm64_const as r

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import BASE, ROOT, OBJECT, PRIMARY, OWNER, STACK, STUB, KMD_SHA256

DEVICE, CONTEXT, POOL = OWNER+0x1000, OWNER+0x2000, OWNER+0x3000
REQUEST, NODES = PRIMARY+0x1000, PRIMARY+0x2000


def device_prefix(m, pe, process):
    """Execute CreateDevice through vtable publication, not complete creation."""
    assert m.read(PRIMARY+0x90, 'Q') == (BASE+0x1ab1000,)
    assert m.read(BASE+0x14c1628, 'Q') == (BASE+0x198b500,)
    m.write(OBJECT+0x10, 'II', 5, 2)
    m.write(OBJECT+0x480c, 'B', 1)
    m.write(OBJECT+0x1988, 'Q', POOL)
    m.write(REQUEST, 'QQIIQ', 0xabcdef, 0, 7, 0, process)
    entries = (0x1ab1000, 0x198b500, 0x19b0a10, 0xdca010,
               0x19d9700, 0x1a40510, 0x1a3ff08, 0x1a24540, 0xdc24b8)
    path, allocations = [], []

    def hook(_, pc, size, user):
        if pc-BASE in entries:
            path.append(hex(pc-BASE))
        if pc == BASE+0xdc24e8:
            assert [m.reg(i) for i in range(4)] == [0x118, POOL, 0x200, 0x444c764e]
            allocations.append(0x118)
            m.ret(DEVICE)
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc in (BASE+0xce478, BASE+0xce4b0, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            raise AssertionError(('device vendor assertion', hex(m.reg(30)-BASE)))

    handle = m.u.hook_add(UC_HOOK_CODE, hook)
    m.u.reg_write(r.UC_ARM64_REG_X0, OBJECT)
    m.u.reg_write(r.UC_ARM64_REG_X1, REQUEST)
    m.u.reg_write(r.UC_ARM64_REG_SP, STACK+0xf000)
    m.u.reg_write(r.UC_ARM64_REG_X30, STUB+0xff0)
    m.u.emu_start(BASE+0x1ab1000, BASE+0x1a3ff40, count=50000)
    assert m.u.reg_read(r.UC_ARM64_REG_PC) == BASE+0x1a3ff40
    m.u.hook_del(handle)
    assert path == [hex(a) for a in entries] and allocations == [0x118]
    assert m.read(DEVICE, 'Q') == (BASE+0xea0f08,)
    assert m.read(DEVICE+0x90, 'QQ') == (OBJECT, process)
    assert struct.unpack('<Q', pe.get_data(0xea0f08+0xa0, 8)) == (BASE+0x8c7e0,)
    # The entire getter is two instructions. Execute it with three distinct
    # handles, including null; no device write or additional call is involved.
    assert pe.get_data(0x8c7e0, 8) == bytes.fromhex('004c40f9c0035fd6')
    getters = []
    for value in (process, 0, 0xffff800012345678):
        m.write(DEVICE+0x98, 'Q', value)
        before = bytes(m.u.mem_read(DEVICE, 0x118))
        assert m.call(0x8c7e0, DEVICE) == value
        assert bytes(m.u.mem_read(DEVICE, 0x118)) == before
        getters.append(hex(value))
    m.write(DEVICE+0x98, 'Q', process)
    return dict(original_path=path, constructor_stop='0x1a3ff40',
                getter_slot='0xa0', getter_rva='0x8c7e0', process_handles=getters,
                modeled_boundaries=['pool allocation', 'timestamps', 'logging'])


def run(pe, node, engine):
    m = DispatchMachine(pe)
    device = device_prefix(m, pe, OWNER+0x4000)
    assert m.read(PRIMARY+0x1d0, 'Q') == (BASE+0x1ab0fd0,)
    assert m.read(BASE+0x14c1768, 'Q') == (BASE+0x198b190,)
    m.write(OBJECT+0x480c, 'B', 1)
    m.write(OBJECT+0xee0, 'Q', NODES)
    m.write(OBJECT+0xefc, 'II', 8, 8)
    m.write(OBJECT+0x1988, 'Q', POOL)
    m.write(NODES+0x10, 'I', 4)
    m.write(REQUEST, 'Q', 0x12345678)
    m.write(REQUEST+8, 'II', node, 1 << engine)
    for slot, entry in ((0x28, 0x1a40360), (0x50, 0x96e80)):
        assert struct.unpack('<Q', pe.get_data(0xea0f08+slot, 8)) == (BASE+entry,)
    imports = {entry.address-BASE: entry.name.decode()
               for dll in pe.DIRECTORY_ENTRY_IMPORT for entry in dll.imports if entry.name}
    os_boundaries = {}
    for i, (iat, name) in enumerate(((0xe3e4d0, 'KeInitializeGuardedMutex'),
                                    (0xe3e180, 'KeInitializeEvent'),
                                    (0xe3e9b0, 'KeInitializeMutex'))):
        assert imports[iat] == name
        stub = STUB+0x40+4*i
        m.write(BASE+iat, 'Q', stub)
        os_boundaries[stub] = name
    path, calls = [], []
    entries = (0x1ab0fd0, 0x198b190, 0x1a40360, 0x19e0ce0,
               0x1a26038, 0x1a24540, 0xdc24b8, 0x19a9d10)

    def hook(_, pc, size, user):
        if pc-BASE in entries:
            path.append(hex(pc-BASE))
        if pc == BASE+0xdc24e8:
            assert [m.reg(i) for i in range(4)] == [0x2d0, POOL, 0x200, 0x544c764e]
            calls.append('pool_allocate_0x2d0')
            m.ret(CONTEXT)
        elif pc in os_boundaries:
            name = os_boundaries[pc]
            expected = dict(KeInitializeGuardedMutex=(CONTEXT+0x140,),
                            KeInitializeEvent=(CONTEXT+0x1b8, 0, 1),
                            KeInitializeMutex=(CONTEXT+0x178, 0))[name]
            assert tuple(m.reg(i) for i in range(len(expected))) == expected
            calls.append(name)
            m.ret(0)
        elif pc == BASE+0xcd148:
            assert [m.reg(i) for i in range(3)] == [CONTEXT+0x200, 0, 16]
            m.u.mem_write(m.reg(0), bytes(m.reg(2)))
            m.ret(m.reg(0))
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc in (BASE+0xce478, BASE+0xce4b0, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            raise AssertionError(('vendor assertion', hex(m.reg(30)-BASE)))

    handle = m.u.hook_add(UC_HOOK_CODE, hook)
    m.u.reg_write(r.UC_ARM64_REG_X0, DEVICE)
    m.u.reg_write(r.UC_ARM64_REG_X1, REQUEST)
    m.u.reg_write(r.UC_ARM64_REG_SP, STACK+0xf000)
    m.u.reg_write(r.UC_ARM64_REG_X30, STUB+0xff0)
    m.u.emu_start(BASE+0x1ab0fd0, BASE+0x19e0d84, count=50000)
    assert m.u.reg_read(r.UC_ARM64_REG_PC) == BASE+0x19e0d84
    assert path == [hex(a) for a in entries]
    assert m.read(CONTEXT, 'Q') == (BASE+0xe9c628,)
    assert m.read(CONTEXT+0x70, 'I') == (2,)
    assert m.read(CONTEXT+0x78, 'Q') == (DEVICE,)
    assert m.read(CONTEXT+0x80, 'III') == (node, engine, 1 << engine)
    assert m.read(CONTEXT+0x8c, 'I') == (11,)
    assert m.read(CONTEXT+0x98, 'Q') == (0x12345678,)
    assert struct.unpack('<Q', pe.get_data(0xe9c628+0x98, 8)) == (BASE+0x92c30,)
    # The constructor prefix is intentionally not resumed. Each isolated call
    # below uses a fresh top-level stack; no completed DDI result is asserted.
    assert m.call(0x1a2a470, CONTEXT, 2) == 1
    assert m.read(CONTEXT+0x8c, 'I') == (2,)
    writes = []
    memory_hook = m.u.hook_add(UC_HOOK_MEM_WRITE,
        lambda _, access, address, size, value, user: writes.append((address-CONTEXT, size, value)),
        begin=CONTEXT, end=CONTEXT+0x2cf)
    counters = []
    for previous in (0, 1, 0x7fffffff, 0xffffffff):
        m.write(CONTEXT+0x6c, 'I', previous)
        before = bytes(m.u.mem_read(CONTEXT, 0x2d0))
        writes.clear()
        m.call(0x92c30, CONTEXT, 0)
        after = bytearray(before)
        value = (previous+1) & 0xffffffff
        after[0x6c:0x70] = value.to_bytes(4, 'little')
        assert bytes(m.u.mem_read(CONTEXT, 0x2d0)) == after
        assert writes == [(0x6c, 4, value)]
        counters.append(dict(before=previous, after=value))
    m.u.hook_del(memory_hook)
    m.u.hook_del(handle)
    return dict(node=node, engine=engine, device_creation=device,
                original_path=path, modeled_calls=calls,
                context_vtable='0xe9c628', callback_slot='0x98', callback_rva='0x92c30',
                counters=counters, constructor_stop='0x19e0d84',
                context_only_write_offset='0x6c')


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[1])
    cases = [run(pe, node, engine) for node, engine in ((0, 0), (2, 1), (3, 7))]
    print(json.dumps(dict(passed=True, hardware_access=False, kmd_sha256=KMD_SHA256,
        cases=cases, limitations=[
            'Original device creation stops after adapter/process fields and concrete vtable publication',
            'Original context creation stops after base construction and concrete vtable publication',
            'Allocation, OS primitive initialization, memset, timestamps and logging are modeled',
            'Mode 2 is selected explicitly through the original setter, not inferred as a live default',
            'Callback increments a software counter; no engine stop or DMA-quiescence guarantee']), indent=2))


if __name__ == '__main__':
    main()
