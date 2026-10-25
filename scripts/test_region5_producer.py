#!/usr/bin/env python3
"""Trace original raw HAL-1 ring production and read it with the C history code.

Producer entry is invoked explicitly after original reset/registration; this
is not execution of its periodic caller or proof of hardware publication.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from emulate_region5_setup import ALLOC_BYTES, RING_OFFSET, RING_BYTES

LOCAL_RECORD = 0x188000  # Explicit synthetic caller-owned data in mapped RAM.
LOCAL_HEADER = 0x1860f8
STOP = 0x40000000
PRODUCER = 0x106ab6


def machine(blob):
    payload = bytearray(ALLOC_BYTES)
    payload[:16] = struct.pack('<IQI', 5, HOST+RING_OFFSET, RING_BYTES)
    held = []

    def response(u):
        held.append(u)
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, method=0x30b, host_payload=payload, host_handler=response,
                 boot_from_reset=True, via_trap=True, dma_register_model=True)
    assert result['returned'] and result['firmware_code_unmodified']
    assert result['function_stubs'] == [] and result['interrupts'] == [(0xb02, HOST >> 8)]
    return held[0], result


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-producer-') as tmp:
        library = Path(tmp)/'ring.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_ring.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    history = lib.n1x_dla_region5_history
    history.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
    cases = []
    for name, initial, blocked in (
            ('initial_zero_host_header', None, False),
            ('linear', (8, 60), False),
            ('wrap_and_drop_first_cursor', (8, RING_BYTES-52), False),
            ('collision_advance_first_cursor', (112, 60), False),
            ('first_cursor_wrap', (RING_BYTES-52, RING_BYTES-104), False),
            ('blocked_completion_then_resume', None, True)):
        u, state = machine(blob)
        old_header = (0, 0) if initial is None else initial
        first_cursor, second_cursor = (8, 8) if initial is None else initial
        u.mem_write(LOCAL_HEADER, struct.pack('<II', first_cursor, second_cursor))
        u.mem_write(HOST+RING_OFFSET, struct.pack('<II', *old_header))
        next_second = second_cursor+52
        if next_second >= RING_BYTES:
            next_second = 8
        next_first = first_cursor
        if first_cursor == next_second:
            next_first = next_second+52
            if next_first >= RING_BYTES:
                next_first = 8
        new_header = (next_first, next_second)
        record = bytes(range(0x40, 0x40+52))
        u.mem_write(LOCAL_RECORD, record)
        before = bytes(u.mem_read(HOST, ALLOC_BYTES))
        start = len(state['dma_commands'])
        snapshots = []
        wrapped = second_cursor+52 >= RING_BYTES
        write_offset = 8 if wrapped else second_cursor
        target = HOST+RING_OFFSET+write_offset

        def observe(_, access, address, size, value, user):
            if len(state['dma_commands']) > start:
                command = state['dma_commands'][-1]
                snapshots.append({'destination': command['destination'], 'bytes': command['bytes'],
                                  'header': struct.unpack('<II', u.mem_read(HOST+RING_OFFSET, 8)),
                                  'record_complete': bytes(u.mem_read(target, 52)) == record})

        observation = u.hook_add(UC_HOOK_MEM_WRITE, observe,
                                 begin=MMIO+0x118, end=MMIO+0x11b)
        hold = None
        if blocked:
            def hold_completion(_, access, address, size, value, user):
                if len(state['dma_commands']) > start:
                    u.mem_write(MMIO+0x118, bytes(4))
            hold = u.hook_add(UC_HOOK_MEM_READ, hold_completion,
                              begin=MMIO+0x118, end=MMIO+0x11b)
        u.reg_write(regs.UC_RISCV_REG_A0, LOCAL_RECORD)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(PRODUCER, STOP, count=5000 if blocked else 200000)
        if blocked:
            assert u.reg_read(regs.UC_RISCV_REG_PC) != STOP
            issued = state['dma_commands'][start:]
            assert sum(op['bytes'] for op in issued) == 52
            assert all(op['destination'] != HOST+RING_OFFSET for op in issued)
            assert struct.unpack('<II', u.mem_read(HOST+RING_OFFSET, 8)) == old_header
            # The model copies bytes synchronously on command issue. The
            # original helper still waits for completion before publishing.
            assert bytes(u.mem_read(target, 52)) == record
            # Resume the SAME pending operation after releasing its fake status;
            # never restart the producer and accidentally submit a second write.
            u.hook_del(hold)
            u.emu_start(u.reg_read(regs.UC_RISCV_REG_PC), STOP, count=200000)
        u.hook_del(observation)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
        operations = state['dma_commands'][start:]
        assert operations and all(op['direction'] == 'write' for op in operations)
        assert sum(op['bytes'] for op in operations[:-1]) == 52
        assert operations[-1] == {'command': 0x2120, 'source': LOCAL_HEADER,
                                  'destination': HOST+RING_OFFSET, 'bytes': 8, 'direction': 'write'}
        offset = 0
        for operation in operations[:-1]:
            assert operation['source'] == LOCAL_RECORD+offset
            assert operation['destination'] == target+offset
            offset += operation['bytes']
        assert all(tuple(s['header']) == old_header for s in snapshots[:-1])
        assert snapshots[-2]['record_complete'] and tuple(snapshots[-2]['header']) == old_header
        assert snapshots[-1]['record_complete'] and tuple(snapshots[-1]['header']) == new_header
        after = bytes(u.mem_read(HOST, ALLOC_BYTES))
        expected = bytearray(before)
        expected[RING_OFFSET:RING_OFFSET+8] = struct.pack('<II', *new_header)
        expected[RING_OFFSET+write_offset:RING_OFFSET+write_offset+52] = record
        assert after == expected
        assert struct.unpack('<II', u.mem_read(LOCAL_HEADER, 8)) == new_header
        output = ctypes.create_string_buffer(2600)
        count = ctypes.c_uint32()
        assert history(after[RING_OFFSET:RING_OFFSET+RING_BYTES], RING_BYTES,
                       output, len(output), ctypes.byref(count)) == 0
        assert count.value in (1, 2, 50)
        visible = record in [output.raw[i*52:(i+1)*52] for i in range(count.value)]
        assert visible == (not wrapped)
        if not wrapped:
            assert output.raw[count.value*52-52:count.value*52] == record
        overwritten_on_next_call = False
        if wrapped:
            second_record = bytes(range(0x90, 0x90+52))
            u.mem_write(LOCAL_RECORD, second_record)
            u.reg_write(regs.UC_RISCV_REG_A0, LOCAL_RECORD)
            u.reg_write(regs.UC_RISCV_REG_RA, STOP)
            u.emu_start(PRODUCER, STOP, count=200000)
            assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
            following = bytes(u.mem_read(HOST, ALLOC_BYTES))
            expected_following = bytearray(after)
            expected_following[RING_OFFSET:RING_OFFSET+8] = struct.pack('<II', 112, 60)
            expected_following[RING_OFFSET+8:RING_OFFSET+60] = second_record
            assert following == expected_following
            assert record not in following[RING_OFFSET:RING_OFFSET+RING_BYTES]
            assert history(following[RING_OFFSET:RING_OFFSET+RING_BYTES], RING_BYTES,
                           output, len(output), ctypes.byref(count)) == 0
            assert output.raw[count.value*52-52:count.value*52] == second_record
            assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
            overwritten_on_next_call = True
        cases.append({'case': name, 'cursors_before': [first_cursor, second_cursor],
                      'cursors_after': list(new_header), 'dma': operations,
                      'history_count': count.value, 'wrap_write_offset': write_offset,
                      'new_record_visible_in_c_history': visible,
                      'blocked_then_resumed_same_state': blocked,
                      'wrap_record_overwritten_on_next_call': overwritten_on_next_call})
    # An unconfigured descriptor performs no DMA. Return-value semantics on
    # this early exit are not assigned: A0 may still be the record pointer.
    for low, high, length in ((0, 0, RING_BYTES), (HOST+RING_OFFSET, 0, 0)):
        u, state = machine(blob)
        u.mem_write(0x1859d4, struct.pack('<III', low, high, length))
        u.mem_write(LOCAL_RECORD, bytes(52))
        start = len(state['dma_commands'])
        before = bytes(u.mem_read(HOST, ALLOC_BYTES))
        u.reg_write(regs.UC_RISCV_REG_A0, LOCAL_RECORD)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(PRODUCER, STOP, count=200000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        assert len(state['dma_commands']) == start and bytes(u.mem_read(HOST, ALLOC_BYTES)) == before
    print(json.dumps({'passed': True, 'hardware_access': False, 'producer': hex(PRODUCER),
                      'firmware_code_unmodified': True, 'firmware_function_stubs': [],
                      'cases': cases, 'unconfigured_no_dma_controls': 2,
                      'limitations': ['Producer is invoked explicitly, not by its periodic caller',
                                      'DMA completion/coherency and interrupt/reset effects are synthetic',
                                      'A final eight-byte modeled DMA does not prove physical atomicity',
                                      'Record semantics, live stable capture, opaque image identity and inference remain open']}, indent=2))


if __name__ == '__main__':
    main()
