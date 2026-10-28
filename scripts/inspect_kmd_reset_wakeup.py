#!/usr/bin/env python3
"""Resolve and execute the original power-wakeup prerequisite of DLA reset.

The parent, constructors, power policy, lock operations and register accessor
execute unchanged. HFRP, common loading, OS timing and IRQ setup are modeled.
"""
import hashlib
import json

import pefile
from unicorn import UC_HOOK_CODE
from unicorn import arm64_const as regs

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import (BASE, PARENT, PRIMARY, OWNER, STUB,
                                         ROOT, KMD_SHA256)

HFRP, PROVIDER, LINK = OWNER+0x1000, OWNER+0x2000, OWNER+0x3000


def run(pe, *, pending=1, version_word=0x12345678, read_error=0,
        power_error=0, firmware_error=0, loader_error=0, delayed=False,
        query=False, power_state=0, query_error=0, query_firmware_error=0,
        query_size=1):
    m = DispatchMachine(pe)
    m.write(PARENT+0x14e, 'H', 3)
    m.call(0x194fb08, PARENT)
    assert m.read(PARENT+0xe0, 'Q') == (BASE+0xd40060,)
    assert m.read(PARENT+0xc8, 'Q') == (BASE+0xd405f0,)
    assert m.read(PARENT+0x108, 'Q') == (BASE+0xd41630,)
    m.write(OWNER+0x7000, 'Q', PRIMARY)
    assert m.call(0x195d038, OWNER+0x7000, 0, 3, 0x404044, 0x404044) == 0
    # Select the independently recovered HAL-1 table explicitly. The synthetic
    # constructor selector above otherwise installs HAL 2; it is not a chip ID.
    m.write(PRIMARY+0x496, 'H', 1)
    m.call(0x1951238, PRIMARY)
    assert m.read(PRIMARY+0x3b0, 'Q') == (BASE+0xd48890,)
    assert m.read(PRIMARY+0x3d8, 'Q') == (BASE+0x5be0,)
    for address, value in ((PARENT+0x170, PRIMARY), (PARENT+0x300, HFRP),
                           (HFRP+0x60, PROVIDER), (PROVIDER+0x18, LINK)):
        m.write(address, 'Q', value)
    m.write(LINK+8, 'I', 1)
    m.write(LINK+0xc, 'BB', 1, 5 if query else 3)
    m.write(PARENT+0x149, 'B', int(query))
    m.write(PARENT+0x14b, 'B', int(delayed))
    m.write(PARENT+0x4a8, 'I', pending)
    m.write(PARENT+0x4b0, 'I', 7)
    m.write(BASE+0x13ac13c, 'I', 5)
    events, entries = [], []

    def hook(_, pc, size, user):
        if pc in (BASE+0xd40060, BASE+0xd405f0, BASE+0xd41630,
                  BASE+0xd48890, BASE+0x5be0):
            entries.append(hex(pc-BASE))
        if pc in (BASE+0xd3de30, BASE+0xd3dd40):
            assert m.reg(0) == HFRP
            cmd, length = m.reg(1), m.reg(3)
            payload = bytes(m.u.mem_read(m.reg(2), length)) if length else b''
            events.append(['hfrp', hex(cmd), payload.hex()])
            m.write(m.reg(4), 'H', query_firmware_error if cmd == 0x1af else
                    firmware_error if cmd == 0x190 else 0)
            if cmd == 0x1af and query_size:
                m.write(m.reg(5), 'B', power_state)
            m.write(m.reg(6), 'I', query_size if cmd == 0x1af else 0)
            if pc == BASE+0xd3dd40:
                m.write(m.reg(7), 'Q', 1000)
                pointer, = m.read(m.u.reg_read(regs.UC_ARM64_REG_SP), 'Q')
                m.write(pointer, 'Q', 2000)
            m.ret(query_error if cmd == 0x1af else power_error if cmd == 0x190 else 0)
        elif pc == BASE+0xd39878:
            assert (m.reg(0), m.reg(1), m.reg(2)) == (PARENT, 0, 0x80)
            events.append(['read', '0x80', version_word, read_error])
            if not read_error:
                m.write(m.reg(3), 'I', version_word)
            m.ret(read_error)
        elif pc == BASE+0xd439c0:
            assert (m.reg(0), m.reg(1)) == (PARENT, PRIMARY)
            events.append(['loader', loader_error])
            m.ret(loader_error)
        elif pc == BASE+0xd496e0:
            assert (m.reg(0), m.reg(1)) == (PARENT, PRIMARY)
            events.append(['postinit_23'])
            m.ret(0)
        elif pc == BASE+0xd49800:
            assert (m.reg(0), m.reg(1)) == (PARENT, PRIMARY)
            events.append(['irq_setup', m.reg(2)])
            m.ret(0)
        elif pc == BASE+0xd39ef0:
            events.append(['notification', m.reg(2), m.reg(3)])
            m.ret(0)
        elif pc == BASE+0x238330:
            m.ret(0x1234)  # Synthetic owner identity; atomic lock code executes.
        elif pc in (BASE+0xd38a38, BASE+0xd38758, BASE+0xd38970,
                     BASE+0xd3f458, BASE+0xd3f4f0, BASE+0xce440, BASE+0xce4b0):
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    status = m.call(0xd41740, PARENT, 0, 0)
    assert m.read(PARENT+0x4ac, 'I') == (0,)
    return {'status': status, 'events': events, 'entries': entries,
            'pending_after': m.read(PARENT+0x4a8, 'I')[0],
            'loader_busy': m.read(PRIMARY+0x5ac, 'B')[0]}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    suffix = [['hfrp', '0x19b', '03'], ['hfrp', '0x19a', '01'],
              ['hfrp', '0x19b', '0c'], ['loader', 0]]
    # Expectations follow the original call sites: nonzero OS_VERSION skips
    # reload, whereas a zero value or queried power/rail-gating selects it.
    specs = [({}, '8c0000', [1, 2, 4], 'read', False, 0),
             ({'pending': 0}, None, [], None, False, 0),
             ({'version_word': 0}, '8c0000', [1, 2, 4], 'read', True, 0),
             ({'version_word': 1}, '8c0000', [1, 2, 4], 'read', False, 0),
             ({'version_word': 0xffffffff}, '8c0000', [1, 2, 4], 'read', False, 0),
             ({'read_error': 0x65}, '8c0000', [1, 2, 4], 'read', False, 0),
             ({'power_error': 0x65}, '8c0000', [], None, False, 0x65),
             ({'firmware_error': 1}, '8c0000', [], None, False, 0xffff),
             ({'version_word': 0, 'loader_error': 0x65}, '8c0000', [1, 2, 4], 'read', True, 0x65),
             ({'pending': 0x10, 'delayed': True}, '800000', [1], 'read', False, 0),
             ({'pending': 0x60, 'delayed': True}, '0c0000', [2, 4], 'read', False, 0)]
    specs += [({'query': True, 'power_state': state},
               '8c00' if state & 6 else '8000',
               [1, 2, 4] if state & 6 else [1], None, bool(state & 6), 0)
              for state in (0, 1, 2, 4, 7, 0xff)]
    specs += [({'query': True, 'query_error': 0x65}, '8000', [1], None, False, 0)]
    specs += [({'query': True, 'query_firmware_error': 1}, '8000', [1], None, False, 0),
              ({'query': True, 'query_size': 0}, '8000', [1], None, False, 0)]
    for kwargs, payload, notifications, read, reload, status in specs:
        row = run(pe, **kwargs)
        expected = [['hfrp', '0x1af', '']] if kwargs.get('query') else []
        if payload:
            expected.append(['hfrp', '0x190', payload])
            expected += [['notification', bit, 0] for bit in notifications]
        if read:
            expected.append(['read', '0x80', kwargs.get('version_word', 0x12345678),
                             kwargs.get('read_error', 0)])
        wake_error = bool(kwargs.get('power_error') or kwargs.get('firmware_error'))
        if payload and not wake_error:
            expected.append(['loader', kwargs.get('loader_error', 0)] if reload else ['postinit_23'])
            expected.append(['irq_setup', 0])
        if not status:
            expected += suffix
        assert row['status'] == status, (kwargs, row)
        assert row['events'] == expected, (kwargs, row)
        assert row['pending_after'] == (1 if wake_error else 0), (kwargs, row)
        assert row['loader_busy'] == int(wake_error), (kwargs, row)
        assert ('0x5be0' in row['entries']) == reload
        assert ('0xd48890' in row['entries']) == bool(read)
        assert ('0xd41630' in row['entries']) == bool(kwargs.get('query'))
        assert ('0xd40060' in row['entries']) == (kwargs.get('pending', 1) != 0)
        rows.append({'inputs': kwargs, **row})
    print(json.dumps({'passed': True, 'hardware_access': False,
        'kmd_sha256': KMD_SHA256, 'cases': rows,
        'limitations': ['Outer profile 3 and primary profile 1 are explicit fixtures, not live Windows discovery',
            'Original wakeup, query/power policy, accessor and atomic lock code execute',
            'HFRP responses, OS identity/time, common loader and IRQ/postinit bodies are modeled',
            'No hardware power transition, reset, firmware loading or DMA quiescence is established']}, indent=2))


if __name__ == '__main__':
    main()
