#!/usr/bin/env python3
"""Recover P16 preemption gates and idle completion using original RV32 code.

Boot/trap/scheduler execute unchanged. MMIO, DMA and host ACK are synthetic.
Active-task engine preemption and physical DMA quiescence are not tested.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ

from emulate_profile1_commands import run
from emulate_firmware_commands import ROOT, MMIO, ACK
from extract_profile1_firmware import PROFILE1_HASH
from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import BASE, PARENT, PRIMARY, OWNER, OBJECT, KMD_SHA256

STATE, OUTSTANDING, ENABLE = 0x1862a4, 0x183720, 0x183728


def producer(pe, dma, error):
    m = DispatchMachine(pe)
    m.write(OWNER+0x7000, 'Q', PRIMARY)
    assert m.call(0x195d038, OWNER+0x7000, 0, 3, 0x404044, 0x404044) == 0
    assert m.read(PRIMARY+0xf8, 'Q') == (BASE+0xd49fc0,)
    m.write(BASE+0x1512148, 'Q', OWNER)
    m.write(OBJECT, 'QQ', 0xabcdef, dma)
    sends = []

    def hook(_, pc, size, user):
        if pc == BASE+0xd39160:
            m.ret(0)  # lookup result is unused by this producer
        elif pc == BASE+0xd3af88:
            m.ret(1)
        elif pc in (BASE+0xd3af28, BASE+0xd3afc8, BASE+0xd38a38,
                     BASE+0xd38758, BASE+0xce440, BASE+0xce4b0,
                     BASE+0xd38970, BASE+0x2385c0):
            m.ret(0)
        elif pc == BASE+0xd4c800:
            assert [m.reg(i) for i in range(5)] == [PARENT, PRIMARY+8, dma >> 8, 0x212, 0]
            sends.append({'method': hex(m.reg(3)), 'data': hex(m.reg(2))})
            m.ret(error)
        elif pc == BASE+0xdf8f38:
            raise AssertionError('unexpected vendor assertion')

    m.u.hook_add(UC_HOOK_CODE, hook)
    result = m.call(0xd482f0, PARENT, PRIMARY, OBJECT)
    # The selected original method wrapper ignores the final sender's status.
    expected = 0 if dma >> 8 else 0x1f
    assert result == expected and len(sends) == int(bool(dma >> 8)), (dma, error, result, sends)
    pending = m.read(PARENT+0x4c0, 'Q')[0]
    assert pending == (0xabcdef if not expected else 0)
    return {'dma': hex(dma), 'sender_error': error, 'return': result,
            'sends': sends, 'retained_owner': hex(pending)}


def check(blob, commands, *, state=0, enabled=0, outstanding=0, resume=False):
    machines, snapshots, accesses = [], [], []

    def memory(u, access, address, size, value, user):
        if MMIO <= address < MMIO+0x40000:
            accesses.append(('read' if access == UC_MEM_READ else 'write', address-MMIO))

    def submit(u):
        if not machines:
            assert int.from_bytes(u.mem_read(STATE, 4), 'little') == 0
            assert bytes(u.mem_read(OUTSTANDING, 1)) == b'\0'
            u.mem_write(STATE, struct.pack('<I', state))
            u.mem_write(OUTSTANDING, bytes([outstanding]))
            u.mem_write(ENABLE, bytes([enabled]))
            u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory)
        command, data = commands[len(machines)]
        machines.append(u)
        u.mem_write(MMIO+0x68, struct.pack('<II', 0x300 | command, data))

    def host_ack(u):
        snapshots.append({'response': list(struct.unpack('<II', u.mem_read(MMIO+0x40, 8))),
                          'state': int.from_bytes(u.mem_read(STATE, 4), 'little'),
                          'outstanding': int.from_bytes(u.mem_read(OUTSTANDING, 1), 'little'),
                          'enabled': int.from_bytes(u.mem_read(ENABLE, 1), 'little')})
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, sequence=[0]*len(commands), host_submit=submit,
                 host_handler=host_ack, boot_from_reset=True, via_trap=True,
                 dma_register_model=True, resume_scheduler=resume)
    assert result['returned'] and result['firmware_code_unmodified']
    assert not result['function_stubs'] and not result['dma_commands']
    assert len(result['trap_returns']) == len(commands)
    assert result['scheduler_wfi'] == resume
    # These gate/idle cases must not program or poll arithmetic engine blocks.
    assert all(offset < 0x2000 for _, offset in accesses)
    final = {'state': int.from_bytes(machines[-1].mem_read(STATE, 4), 'little'),
             'outstanding': int.from_bytes(machines[-1].mem_read(OUTSTANDING, 1), 'little'),
             'enabled': int.from_bytes(machines[-1].mem_read(ENABLE, 1), 'little')}
    return {'commands': commands, 'initial': [state, enabled, outstanding],
            'resume_scheduler': resume, 'notifications': snapshots, 'final': final,
            'register_offsets': sorted({hex(offset) for _, offset in accesses}),
            'dma_transfers': 0, 'code_unmodified': True}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    producers = [producer(pe, dma, error) for dma, error in
                 ((0, 0), (255, 0), (256, 0), (256, 0x65),
                  (0x1234567800, 0), (0x1234567800, 0x65))]
    rows = []
    for data in (0, 1, 2, 3, 255, 256):
        r = check(blob, [(28, data)])
        assert r['notifications'] == [{'response': [0x1c02, data], 'state': 0,
                                       'outstanding': 0, 'enabled': data & 1}]
        rows.append(r)
    for state in range(5):
        for outstanding in (0, 1):
            if not state and not outstanding:
                continue
            r = check(blob, [(28, 0)], state=state, enabled=1, outstanding=outstanding)
            assert r['notifications'] == [{'response': [0xc1c01, 0], 'state': state,
                                           'outstanding': outstanding, 'enabled': 1}]
            rows.append(r)
    for state in range(5):
        for enabled, outstanding in ((0, 0), (1, 1), (1, 0)):
            if enabled and not outstanding and state not in (2, 3):
                continue  # Active-task branches need a valid task/engine model.
            r = check(blob, [(18, 0)], state=state, enabled=enabled, outstanding=outstanding)
            assert r['notifications'] == [{'response': [0xc1201, 0], 'state': state,
                                           'outstanding': outstanding, 'enabled': enabled}]
            rows.append(r)
    r = check(blob, [(17, 0)])
    assert r['notifications'][0]['response'] == [0x11101, 0]
    assert r['final'] == {'state': 0, 'outstanding': 0, 'enabled': 0}
    rows.append(r)
    for resume in (False, True):
        r = check(blob, [(28, 1), (18, 0), (18, 0), (28, 0)], resume=resume)
        expected = [[0x1c02, 1], [0x1202, 0], [0xc1201, 0], [0xc1c01, 0]]
        if resume:
            expected += [[0x120b, 0]]
        assert [n['response'] for n in r['notifications']] == expected
        assert [n['outstanding'] for n in r['notifications'][:4]] == [0, 1, 1, 1]
        if resume:
            assert r['notifications'][-1]['outstanding'] == 1
        assert r['final'] == {'state': 0, 'outstanding': int(not resume), 'enabled': 1}
        rows.append(r)
    print(json.dumps({'passed': True, 'firmware_sha256': PROFILE1_HASH,
                      'kmd_sha256': KMD_SHA256, 'producer_cases': producers,
                      'cases': rows, 'case_count': len(rows), 'hardware_access': False,
                      'limitations': ['Only idle acceptance and synthetic rejection states',
                                      'No active-task preemption or engine DMA model',
                                      'No evidence of physical reset or safe buffer reclamation']}, indent=2))


if __name__ == '__main__':
    main()
