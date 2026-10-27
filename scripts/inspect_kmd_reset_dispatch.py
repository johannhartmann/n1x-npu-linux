#!/usr/bin/env python3
"""Recover original Windows DDI registration and DLA reset routing offline.

Executes the ordinary initialization-table builders, not DriverEntry or the
conditional alternate initializer. Reset bodies are stopping boundaries: this
test establishes dispatch identity, never hardware quiescence.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED

from inspect_kmd_task_submission import (Machine, BASE, OBJECT, PARENT,
                                         PRIMARY, ROOT, KMD_SHA256)

# ARM64 DRIVER_INITIALIZATION_DATA offsets, including reserved pointer slots.
# https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/dispmprt/ns-dispmprt-_driver_initialization_data
# name, public-table offset, wrapper RVA, implementation-global offset or None,
# implementation RVA. Globals are relative to 0x14c1570.
CALLBACKS = (
    ('StopDevice', 0x18, 0x1ab0d30, 0x40, 0x1aa1e90),
    ('ResetDevice', 0x68, 0xe312f0, 0x90, 0xaa4c0),
    ('ResetFromTimeout', 0x100, 0x1ab1700, 0x128, 0x19944e0),
    ('RestartFromTimeout', 0x108, 0x1ab1730, 0x130, 0x19946f0),
    ('ResetEngine', 0x250, 0x1ab16f0, None, 0x1994260),
    ('SubmitCommandVirtual', 0x2d0, 0x1ab19d0, None, 0xdb57e0),
    ('ResetHwEngine', 0x3a0, 0x1ab1b60, 0x3c8, 0x199a600),
    ('ResumeHwEngine', 0x478, 0x1ab1bc0, 0x4a0, 0x199aac0),
)


class DispatchMachine(Machine):
    def __init__(self, pe):
        super().__init__(pe)

        def missing(u, access, address, size, value, user):
            if BASE <= address < BASE+pe.OPTIONAL_HEADER.SizeOfImage:
                page = address & ~4095
                u.mem_map(page, 4096)
                u.mem_write(page, pe.get_data(page-BASE, 4096))
                return True
            return False

        self.u.hook_add(UC_HOOK_MEM_UNMAPPED, missing)
        self.call(0x6e96718)
        self.call(0x6e95680, PRIMARY)


def registration(pe):
    results = []
    for name, offset, wrapper, global_offset, expected in CALLBACKS:
        m = DispatchMachine(pe)
        assert m.read(PRIMARY+offset, 'Q') == (BASE+wrapper,)
        target = expected if global_offset is None else m.read(
            BASE+0x14c1570+global_offset, 'Q')[0]-BASE
        if expected is not None:
            assert target == expected
        assert 0 < target < pe.OPTIONAL_HEADER.SizeOfImage
        calls = []

        def hook(_, pc, size, user):
            if pc == BASE+target:
                calls.append([m.reg(i) for i in range(4)])
                m.ret(0x12345678)

        m.u.hook_add(UC_HOOK_CODE, hook)
        assert m.call(wrapper, 11, 22, 33, 44) == 0x12345678
        assert calls == [[11, 22, 33, 44]]
        results.append({'callback': name, 'offset': hex(offset),
                        'wrapper': hex(wrapper), 'target': hex(target)})
    return results


def routing(pe, timeout, status):
    m = DispatchMachine(pe)
    m.write(OBJECT, 'Q', BASE+0xe9c050)
    m.write(OBJECT+0x10, 'II', 5, 2)
    m.write(OBJECT+0x480c, 'B', 1)
    m.write(OBJECT+0x52e0, 'Q', PARENT)
    assert pe.get_data(0xe9c3c8, 8) == (BASE+0x91260).to_bytes(8, 'little')
    # Real scheduler vtable, including slot 0x40 -> dd52f0.
    m.write(PARENT, 'Q', BASE+0xe9c910)
    m.write(PRIMARY+0x1000, 'III', 2, 1, 0xdeadbeef)
    boundary = 0x19d9f80 if timeout else 0xdd52f0
    calls = []

    def hook(_, pc, size, user):
        if pc == BASE+boundary:
            if timeout:
                assert m.reg(0) == OBJECT
            else:
                assert [m.reg(i) for i in (0, 1)] == [PARENT, PRIMARY+0x1000]
                # The original caller initializes the output flag to zero.
                assert m.read(m.reg(2), 'B') == (0,)
            calls.append(hex(boundary))
            m.ret(status)
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc in (BASE+0xce478, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            raise AssertionError('unexpected vendor assertion')

    m.u.hook_add(UC_HOOK_CODE, hook)
    entry = 0x1ab1700 if timeout else 0x1ab16f0
    result = m.call(entry, OBJECT, PRIMARY+0x1000)
    assert result == status and calls == [hex(boundary)]
    return {'callback': 'ResetFromTimeout' if timeout else 'ResetEngine',
            'adapter_class': [5, 2], 'stopped_before': hex(boundary),
            'returned_status': hex(result)}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    callbacks = registration(pe)
    routes = [routing(pe, timeout, status) for timeout in (False, True)
              for status in (0, 0xc00000bb)]
    print(json.dumps({'callbacks': callbacks, 'routing': routes,
                      'cases': len(callbacks)+len(routes),
                      'physical_reset_executed': False}, indent=2))


if __name__ == '__main__':
    main()
