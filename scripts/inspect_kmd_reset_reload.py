#!/usr/bin/env python3
"""Join the original EngineReset RM packet to HFRP reset/reload policy.

The job object's accessor, generic RM dispatch, register I/O, HFRP transport
and the final common loader are modeled boundaries. No hardware is accessed.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, arm64_const as regs

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import (BASE, OBJECT, PARENT, PRIMARY, OWNER, JOB,
                                         STUB, ROOT, KMD_SHA256)

HFRP, PROVIDER, LINK = (OWNER+i*0x1000 for i in (1, 2, 3))


def run(pe, gate=0, enabled=True, capability=1, pending_error=None,
        transport_error=0, firmware_error=0, loader_error=0,
        register_error_at=None, via_job=True, missing_owner=False):
    m = DispatchMachine(pe)
    m.write(OWNER+0x7000, 'Q', PRIMARY)
    assert m.call(0x195d038, OWNER+0x7000, 0, 3, 0x404044, 0x404044) == 0
    assert m.read(PRIMARY+0x210, 'Q') == (BASE+0xd48b30,)
    table = struct.unpack('<QQIIQ', pe.get_data(0x108e840, 32))
    assert table == (BASE+0xd44e40, 0, 0x35800005, 2, BASE+0x108ec50)
    m.write(BASE+0x1512148, 'Q', OWNER)
    m.write(BASE+0x149ba3c, 'I', gate)
    m.write(OWNER+0x58, 'Q', PARENT)
    m.write(PARENT+0x170, 'Q', PRIMARY)
    m.write(PARENT+0x300, 'Q', HFRP)
    m.write(HFRP+0x60, 'Q', PROVIDER)
    m.write(PROVIDER+0x18, 'Q', LINK)
    m.write(LINK+8, 'I', capability)
    m.write(PARENT+0x4a8, 'I', int(pending_error is not None))
    m.write(PARENT+0xe0, 'Q', STUB+8)
    m.write(PRIMARY+0x470, 'I', int(enabled))
    m.write(OBJECT+8, 'I', 0x1234)
    m.write(OBJECT+0x10, 'Q', OWNER+0x5000)
    m.write(JOB+8, 'Q', OWNER+0x6000)
    m.write(OWNER+0x6000, 'Q', OWNER+0x6100)
    m.write(OWNER+0x6100+0xa0, 'Q', STUB+12)
    m.write(OWNER+0x6200+0x198, 'Q', OBJECT)
    events, packet, pending, register_reads = [], [], [], []
    entries = []

    def hook(_, pc, size, user):
        if pc in (BASE+0x19e2628, BASE+0xdd1960, BASE+0xdd1c10,
                  BASE+0xd44e40, BASE+0xd48b30, BASE+0xd41740):
            entries.append(hex(pc-BASE))
        if pc == BASE+0xd37450:
            header, hints = m.reg(0), m.reg(1)
            client, obj, cmd, flags, payload, length, status = m.read(header, 'IIIIQII')
            assert (client, obj, cmd, flags, length, status) == (
                0x1234, 0x35800000, 0x35800005, 0, 2, 0x56)
            assert bytes(m.u.mem_read(payload, length)) == b'\0\0'
            assert m.read(hints, 'IIQ') == (2, 1, OWNER+0x5000)
            packet.append({'command': hex(cmd), 'object': hex(obj), 'payload': '0000'})
            pending.append((header, m.reg(30)))
            m.u.reg_write(regs.UC_ARM64_REG_X1, payload)
            m.u.reg_write(regs.UC_ARM64_REG_LR, STUB+4)
            m.u.reg_write(regs.UC_ARM64_REG_PC, table[0])
        elif pc == STUB+4:
            header, resume = pending.pop()
            m.write(header+0x1c, 'I', m.reg(0)&0xffffffff)
            m.u.reg_write(regs.UC_ARM64_REG_PC, resume)
        elif pc == STUB+8:
            assert m.reg(0) == PARENT
            events.append(['pending', pending_error])
            m.ret(pending_error)
        elif pc == STUB+12:
            assert m.reg(0) == OWNER+0x6000
            m.ret(0 if missing_owner else OWNER+0x6200)
        elif pc == BASE+0xd39878:
            assert m.reg(0) == PARENT and m.reg(1) == 0
            register_reads.append(hex(m.reg(2)))
            m.write(m.reg(3), 'I', 0)
            m.ret(0)
        elif pc == BASE+0xd39f38:
            assert m.reg(0) == PARENT and m.reg(1) == 0
            events.append(['register_write', hex(m.reg(2)), m.reg(3)])
            m.ret(0x65 if m.reg(2) == register_error_at else 0)
        elif pc == BASE+0xd3de30:
            assert m.reg(0) == HFRP and m.reg(3) == 1
            events.append(['hfrp', hex(m.reg(1)), bytes(m.u.mem_read(m.reg(2), 1)).hex()])
            m.write(m.reg(4), 'H', firmware_error)
            m.write(m.reg(5), 'B', 0)
            m.write(m.reg(6), 'I', 0)
            m.ret(transport_error)
        elif pc == BASE+0xd439c0:
            assert [m.reg(i) for i in (0, 1)] == [PARENT, PRIMARY]
            events.append(['common_loader', loader_error])
            m.ret(loader_error)
        elif pc == BASE+0xc520:
            m.write(m.reg(0), 'Q', 0x12345678)
            m.ret(0)
        elif pc in (BASE+0xd38a38, BASE+0xd38758, BASE+0xce478,
                     BASE+0xce4b0, BASE+0xe075b0):
            m.ret(0)
        elif pc == BASE+0xdf8f38:
            raise AssertionError('unexpected vendor assertion')

    m.u.hook_add(UC_HOOK_CODE, hook)
    result = (m.call(0x19e2628, 0, JOB) if via_job else
              m.call(0xdd1960, OBJECT, OWNER+0x6000))
    assert not pending
    expected = []
    if via_job and missing_owner:
        assert not packet and result == 0
        assert entries == ['0x19e2628']
    elif gate:
        assert not packet and result == 0
    elif not enabled:
        assert len(packet) == 1 and result == 1
    else:
        expected.append(['register_write', '0xd24', 0x80])
        if pending_error is not None:
            expected.append(['pending', pending_error])
        if not pending_error:
            if capability == 1:
                expected += [['hfrp', '0x19b', '03'], ['hfrp', '0x19a', '01'],
                             ['hfrp', '0x19b', '0c']]
            expected.append(['common_loader', loader_error])
        expected.append(['register_write', '0xd20', 0x80])
        assert len(packet) == 1
        assert result == int(not (pending_error or loader_error))
    assert events == expected, events
    expected_entries = ['0x19e2628'] if via_job else []
    if not (via_job and missing_owner):
        expected_entries.append('0xdd1960')
        if not gate:
            expected_entries += ['0xdd1c10', '0xd44e40', '0xd48b30']
            if enabled:
                expected_entries.append('0xd41740')
    assert entries == expected_entries, entries
    assert m.read(PRIMARY+0x5ac, 'B') == (int(bool(expected)),)
    return {'inputs': {'gate': gate, 'enabled': enabled, 'capability': capability,
                       'pending_error': pending_error, 'transport_error': transport_error,
                       'firmware_error': firmware_error, 'loader_error': loader_error,
                       'register_error_at': register_error_at, 'via_job': via_job,
                       'missing_owner': missing_owner},
            'packet': packet, 'events': events, 'return': result,
            'diagnostic_reads': register_reads, 'original_entries': entries}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = [run(pe, **args) for args in (
        {}, {'gate': 1}, {'enabled': False}, {'capability': 0}, {'capability': 2},
        {'pending_error': 0}, {'pending_error': 0x65},
        {'transport_error': 0x65}, {'firmware_error': 0x1234},
        {'loader_error': 0x65}, {'transport_error': 0x65, 'loader_error': 0x65},
        {'register_error_at': 0xd24}, {'register_error_at': 0xd20},
        {'register_error_at': 0xd24, 'loader_error': 0x65},
        {'register_error_at': 0xd20, 'pending_error': 0x65},
        {'missing_owner': True}, {'via_job': False})]
    print(json.dumps({'passed': True, 'kmd_sha256': KMD_SHA256,
                      'cases': cases, 'case_count': len(cases),
                      'modeled_boundaries': ['job owner accessor +0xa0',
                          'generic RM dispatch', 'register I/O', 'HFRP transport',
                          'common loader', 'pending callback', 'logging and timing'],
                      'physical_reset_executed': False}, indent=2))


if __name__ == '__main__':
    main()
