#!/usr/bin/env python3
"""Join original Windows preemption serialization to the P16 firmware.

Original KMD builder and RV32 boot/trap/scheduler execute. Object state, MMIO,
DMA copies and host ACK are synthetic. There is no active arithmetic task.
"""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE, riscv_const as regs

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import (BASE, OBJECT, PARENT, PRIMARY, OWNER,
                                         DESCRIPTOR, JOB, ROOT, KMD_SHA256)
from emulate_profile1_commands import run
from emulate_firmware_commands import HOST, MMIO, ACK
from extract_profile1_firmware import PROFILE1_HASH


def build(pe, sequence=8, submitted=5, node=0):
    m = DispatchMachine(pe)
    m.write(OBJECT, 'Q', BASE+0xe9cb68)
    m.write(OBJECT+0x18, 'I', 0x500)
    m.write(OBJECT+0x20, 'QQQ', PARENT, HOST, DESCRIPTOR)
    m.write(PARENT+0x6ec0, 'Q', PRIMARY)
    m.write(PRIMARY+0xed8, 'Q', OWNER)
    m.write(OWNER+0x18, 'Q', OWNER+0x1000)
    m.write(OWNER+0x1000+0x4e0, 'QQ', OWNER+0x2000, HOST+0xf000)
    m.write(OWNER+0x2000+0xefc, 'I', 8)
    m.write(OWNER+0x1000+0x514, 'III', 16, 256, 4)
    m.write(PARENT+8+node*64, 'Q', JOB)
    m.write(PARENT+0xf9c+node*32, 'I', submitted)
    m.write(PARENT+0x7d8+node*32, 'I', 32)
    m.write(JOB+(submitted%32)*0x1f0+0x1e8, 'I', 12)
    assert m.call(0xdd7640, OBJECT, node, sequence) == 0
    assert m.call(0xdd6e40, OBJECT, node, sequence) == 0
    header = m.call(0xdd65a8, OBJECT, sequence)-DESCRIPTOR
    post = m.call(0xdd6ff0, OBJECT, sequence)-DESCRIPTOR
    post_gpu = m.call(0xdd70f0, OBJECT, sequence)
    assert post_gpu == HOST+post
    payload = bytearray(m.u.mem_read(DESCRIPTOR, 0x10000))
    fence = 0xf040+node*32
    expected = bytearray(32)
    expected[0] = 1
    struct.pack_into('<HQHQ', expected, 1, 0, sequence, 27, post_gpu)
    assert payload[header:header+32] == expected
    actions = (b'\x80'+struct.pack('<QI', HOST+fence, submitted)+
               b'\x80'+struct.pack('<QI', HOST+fence, sequence)+b'\0')
    assert payload[post:post+27] == actions
    assert (HOST+header)%256 == 0  # only these aligned slots are submitted here
    struct.pack_into('<I', payload, fence, 0xa5a5a5a5)
    return payload, header, post, fence


def consume(blob, payload, header, fence, method_flags=0x200):
    snapshots, post_status, fence_writes, machines = [], [], [], []

    def code(u, pc, size, user):
        if pc == 0x105d2e:  # return from original postaction interpreter
            value = u.reg_read(regs.UC_RISCV_REG_A0)
            post_status.append(value if value < 1 << 31 else value-(1 << 32))

    def write(u, access, address, size, value, user):
        if address == MMIO+0x118 and value & 0x20:
            # Installed after the RAM-DMA backend: its copy has taken effect.
            fence_writes.append(int.from_bytes(u.mem_read(HOST+fence, 4), 'little'))

    def submit(u):
        machines.append(u)
        u.hook_add(UC_HOOK_CODE, code)
        u.hook_add(UC_HOOK_MEM_WRITE, write)
        assert bytes(u.mem_read(0x1862a4, 4)) == bytes(4)
        u.mem_write(0x183728, b'\x01')
        u.mem_write(MMIO+0x68, struct.pack('<II', method_flags | 18, (HOST+header) >> 8))

    def ack(u):
        snapshots.append({'mailbox': list(struct.unpack('<II', u.mem_read(MMIO+0x40, 8))),
                          'fence': int.from_bytes(u.mem_read(HOST+fence, 4), 'little'),
                          'outstanding': int.from_bytes(u.mem_read(0x183720, 1), 'little')})
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, host_submit=submit, host_handler=ack, host_payload=payload,
                 boot_from_reset=True, via_trap=True, dma_register_model=True,
                 resume_scheduler=True)
    assert result['returned'] and result['scheduler_wfi']
    assert result['firmware_code_unmodified'] and not result['function_stubs']
    assert not result['scheduler_engine_reads']
    assert all(offset < 0x2000 for offset, _, _ in result['scheduler_mmio_writes'])
    result['postaction_status'] = post_status
    result['fence_writes'] = fence_writes
    result['preemption_outstanding_final'] = int.from_bytes(machines[0].mem_read(0x183720, 1), 'little')
    assert result['preemption_outstanding_final'] == 0
    return result, snapshots


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    inputs = ((0, 0, 0), (8, 5, 0), (16, 31, 1), (24, 32, 2),
              (32, 0xffffffff, 3), (0xfffffff8, 0x12345678, 4))
    for sequence, submitted, node, flags in ((*item, flags) for item in inputs
                                            for flags in (0x200, 0x300)):
        payload, header, post, fence = build(pe, sequence, submitted, node)
        result, snapshots = consume(blob, payload, header, fence, flags)
        final = bytes.fromhex(result['host_payload_final'])
        expected = bytearray(payload)
        struct.pack_into('<I', expected, fence, sequence)
        assert final == expected
        expected_notifications = ([{'mailbox': [0x1202, (HOST+header) >> 8],
                                     'fence': 0xa5a5a5a5, 'outstanding': 1}]
                                  if flags & 0x100 else [])
        expected_notifications.append({'mailbox': [0x120b, 0], 'fence': sequence, 'outstanding': 1})
        assert snapshots == expected_notifications
        dma = result['dma_commands']
        assert [(d['direction'], d['bytes']) for d in dma] == [
            ('read', 32), ('read', 256), ('write', 4), ('write', 4)]
        assert dma[0]['source'] == HOST+header
        assert dma[1]['source'] == (HOST+post) & ~255
        assert [d['destination'] for d in dma[2:]] == [HOST+fence]*2
        assert result['fence_writes'] == [submitted, sequence]
        assert result['postaction_status'] == [0]
        rows.append({'sequence': sequence, 'submitted': submitted, 'node': node,
                     'method': hex(flags | 18),
                     'header_offset': hex(header), 'post_offset': hex(post),
                     'header_hex': payload[header:header+32].hex(),
                     'post_hex': payload[post:post+27].hex(),
                     'notifications': snapshots, 'dma': dma,
                     'fence_writes': result['fence_writes'], 'postaction_status': result['postaction_status']})
    negative = []
    for size in (0, 26, 28):
        payload, header, post, fence = build(pe)
        struct.pack_into('<H', payload, header+11, size)
        result, snapshots = consume(blob, payload, header, fence)
        assert [s['mailbox'] for s in snapshots] == [[0x120b, 0]]
        assert result['postaction_status'] == ([] if not size else [-7])
        assert result['fence_writes'] == ([] if not size else [5, 8])
        expected = bytearray(payload)
        if size:
            struct.pack_into('<I', expected, fence, 8)
        assert bytes.fromhex(result['host_payload_final']) == expected
        negative.append({'post_size': size, 'notifications': snapshots,
                         'postaction_status': result['postaction_status'],
                         'fence_writes': result['fence_writes'],
                         'outstanding_final': result['preemption_outstanding_final']})
    print(json.dumps({'passed': True, 'case_count': len(rows)+len(negative), 'cases': rows,
                      'kmd_sha256': KMD_SHA256, 'firmware_sha256': PROFILE1_HASH,
                      'negative_cases': negative,
                      'hardware_access': False,
                      'limitations': ['Synthetic initialized KMD object graph and empty firmware queue',
                                      'No active-task save/restore or physical DMA quiescence',
                                      'Only aligned request slots are submitted']}, indent=2))


if __name__ == '__main__':
    main()
