#!/usr/bin/env python3
"""Replay pinned Windows task submission and its firmware postaction producer.

Original ARM64 code executes in Unicorn. Generic RM object lookup, OS locks,
logging and the low-level method sender are explicit synthetic boundaries.
The postaction producer executes without stubs for valid indices; its output
is consumed by the original RV32 firmware with RAM-only DMA.
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

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs

from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_profile1_postactions import run as run_postactions, ROOT, HOST, PROFILE1_HASH
from test_task_descriptor import Args

BASE = 0x140000000
OBJECT, PARENT, PRIMARY, OWNER, DESCRIPTOR, JOB, STACK, STUB = (
    0x34000000 + i * 0x10000 for i in range(8))
STOP = STUB + 0xff0


class Machine:
    def __init__(self, pe):
        self.u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        pages = (0xdd1000, 0xdd2000, 0xdd6000, 0xdd7000, 0xd45000,
                 0xd49000, 0xd4a000, 0xd4c000, 0xd57000, 0xd38000,
                 0xd3a000, 0x93000, 0x238000, 0xce000, 0xe3e000,
                 0x1512000, 0x149b000, 0x13ac000,
                 *range(0x1951000, 0x195e000, 0x1000))
        for page in pages:
            self.u.mem_map(BASE + page, 0x1000)
            self.u.mem_write(BASE + page, pe.get_data(page, 0x1000))
        for address in (OBJECT, PARENT, PRIMARY, OWNER, DESCRIPTOR, JOB, STACK, STUB):
            self.u.mem_map(address, 0x10000)
        install_cookie(self.u, pe)
        self.u.mem_write(STUB, bytes.fromhex('c0035fd6') * 4)
        self.write(BASE + 0xe3eaf0, 'Q', STUB)

    def write(self, address, fmt, *values):
        self.u.mem_write(address, struct.pack('<' + fmt, *values))

    def read(self, address, fmt):
        return struct.unpack('<' + fmt, self.u.mem_read(address, struct.calcsize('<' + fmt)))

    def reg(self, index):
        return self.u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def ret(self, value):
        self.u.reg_write(regs.UC_ARM64_REG_X0, value)
        self.u.reg_write(regs.UC_ARM64_REG_PC, self.reg(30))

    def call(self, rva, *args):
        for i, value in enumerate(args):
            self.u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        self.u.reg_write(regs.UC_ARM64_REG_SP, STACK + 0xf000)
        self.u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        self.u.emu_start(BASE + rva, STOP, count=50000)
        assert self.u.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return self.reg(0)


def submit(pe, *, mode=2, descriptor=0x1234567800, sequence=0x12345678,
           gate=0, missing_primary=False, send_error=0, high_level=False):
    m = Machine(pe)
    m.write(PRIMARY + 0x496, 'H', 1)
    m.call(0x1951238, PRIMARY)
    assert m.read(PRIMARY + 0x140, 'Q') == (BASE + 0xd4a400,)
    assert m.read(PRIMARY + 0xf8, 'Q') == (BASE + 0xd49fc0,)
    # Check the dispatch table from the binary rather than inventing its ABI.
    target, zero, command, size, metadata = struct.unpack('<QQIIQ', pe.get_data(0x108e7c0, 32))
    assert (target, zero, command, size) == (BASE + 0xd45050, 0, 0x35800001, 16)
    assert metadata == BASE + 0x108ec50
    m.write(BASE + 0x1512148, 'Q', OWNER)
    m.write(BASE + 0x149ba3c, 'I', gate)
    m.write(PARENT + 0x170, 'Q', 0 if missing_primary else PRIMARY)
    m.write(PRIMARY + 0x68, 'Q', OWNER + 0x1000)
    m.write(PRIMARY + 0x5ac, 'B', 0x35)
    m.write(OBJECT + 8, 'I', 0x1234)
    m.write(OBJECT + 0x10, 'Q', OWNER + 0x2000)
    expected_descriptor = descriptor
    if high_level:
        assert 0 <= sequence <= 0xffffffff
        # Original dd6888 resolves these two virtual callbacks, then constructs
        # the ring address and calls original dd22c8. Descriptor preparation
        # itself remains a stub; the postaction builder is tested separately.
        m.write(JOB + 8, 'Q', OWNER + 0x3000)
        m.write(OWNER + 0x3000, 'Q', OWNER + 0x4000)
        m.write(OWNER + 0x4000 + 0xa0, 'Q', STUB + 8)
        m.write(OWNER + 0x5000 + 0x198, 'Q', OBJECT)
        m.write(OWNER + 0x6000, 'Q', OWNER + 0x8000)
        m.write(OWNER + 0x8000 + 0x60, 'Q', STUB + 12)
        m.write(OWNER + 0x6000 + 0x18, 'I', 0x800)
        m.write(OWNER + 0x6000 + 0x20, 'Q', PARENT)
        m.write(OWNER + 0x6000 + 0x28, 'Q', descriptor)
        m.write(PARENT + 0x6ec0, 'Q', PRIMARY)
        m.write(PRIMARY + 0xee0, 'Q', OWNER + 0x7000)
        m.write(OWNER + 0x7000 + 0x10, 'I', 4)
        m.write(OWNER + 0x7000 + 2 * 0x170 + 0x38, 'I', mode)
        m.write(JOB + 0x20, 'I', 2)
        m.write(JOB + 0x2c, 'I', sequence)
        expected_descriptor += (sequence & 31) * 0x800
    events, envelopes = [], []
    pending = []

    def hook(_, address, instruction_size, user):
        if address == BASE + 0xdd1c10:
            header, context = m.reg(0), m.reg(1)
            client, obj, cmd, flags, params, length, status = m.read(header, 'IIIIQII')
            assert (client, obj, cmd, flags, length, status) == (
                0x1234, 0x35800000 if mode == 1 else 0x35800001,
                command, 0, size, 0x56)
            assert bytes(m.u.mem_read(context, 40)) == struct.pack('<IIQ', 2, 1, OWNER + 0x2000) + bytes(24)
            assert m.read(params, 'QQ') == (expected_descriptor, sequence)
            envelopes.append({'client': client, 'object': hex(obj), 'command': hex(cmd),
                              'descriptor': hex(expected_descriptor), 'sequence': sequence, 'bytes': length})
            # Explicitly replace the generic RM lookup with its checked table target.
            pending.append((header, m.reg(30)))
            m.u.reg_write(regs.UC_ARM64_REG_X1, params)
            m.u.reg_write(regs.UC_ARM64_REG_LR, STUB + 4)
            m.u.reg_write(regs.UC_ARM64_REG_PC, target)
        elif address == STUB + 4:
            header, resume = pending.pop()
            status = m.reg(0) & 0xffffffff
            m.write(header + 0x1c, 'I', status)
            events.append(['handler_status', status])
            m.u.reg_write(regs.UC_ARM64_REG_PC, resume)
        elif address == BASE + 0x93630:
            assert m.reg(0) == OWNER
            m.ret(PARENT)
        elif address == BASE + 0xd38a38:
            m.ret(0x123456)
        elif address == BASE + 0x2385c0:
            m.ret(0x987654)
        elif address == BASE + 0xd3af88:
            assert m.reg(0) == PRIMARY + 0x5b0
            m.ret(1)
        elif address in (BASE + 0xd3af28, BASE + 0xd3afc8):
            assert m.reg(0) in (PRIMARY + 0x5b0, PRIMARY + 0x5b8)
            m.ret(0)
        elif address in (BASE + 0xd38758, BASE + 0xd38970, BASE + 0xce440, BASE + 0xce4b0):
            m.ret(0)
        elif address == BASE + 0xd45180:
            assert [m.reg(i) for i in range(5)] == [PARENT, PRIMARY, 7, (expected_descriptor >> 8) & 0xffffffff, sequence]
            events.append('original_pending_record_cleanup')
            # Execute this helper, including its lock calls, unchanged.
        elif address == BASE + 0xd4c800:
            assert [m.reg(i) for i in range(4)] == [PARENT, OWNER + 0x1000, (expected_descriptor >> 8) & 0xffffffff, 0x207]
            events.append(['method_sender', 0x207, m.reg(2), send_error])
            m.ret(send_error)
        elif address == STUB + 8:
            assert high_level and m.reg(0) == OWNER + 0x3000
            m.ret(OWNER + 0x5000)
        elif address == STUB + 12:
            assert high_level and [m.reg(i) for i in (0, 1)] == [OWNER + 0x6000, JOB]
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    result = (m.call(0xdd6888, OWNER + 0x6000, JOB) if high_level else
              m.call(0xdd22c8, OBJECT, mode, descriptor, sequence))
    assert not pending
    assert m.read(PRIMARY + 0x5ac, 'B') == (0x35,)
    return {'return_value': result, 'envelopes': envelopes, 'events': events,
            'outstanding': m.read(PRIMARY + 0x5b4, 'I')[0]}


def postaction(pe, *, index, multiplier, base_index, stride, value, table=64, fence=HOST):
    m = Machine(pe)
    # submitobject -> adapter -> engine state -> fence provider -> fence owner.
    for address, pointer in ((OBJECT + 0x20, PARENT), (PARENT + 0x6ec0, PRIMARY),
                             (PRIMARY + 0xed8, OWNER), (OWNER + 0x18, OWNER + 0x1000),
                             (OWNER + 0x1000 + 0x4e0, OWNER + 0x2000)):
        m.write(address, 'Q', pointer)
    slot = (multiplier * index) & 0xffffffff
    offset = (((base_index + (stride >> 2) * slot) & 0xffffffff) << 2) & 0xffffffff
    assert slot < 0xffffffff and fence >= offset
    m.write(OWNER + 0x2000 + 0xf00, 'I', multiplier)
    m.write(OWNER + 0x1000 + 0x508, 'III', base_index, 0xffffffff, stride)
    m.write(OWNER + 0x1000 + 0x4e8, 'Q', fence - offset)
    m.write(JOB + 0x20, 'I', index)
    m.write(JOB + 0x2c, 'I', value)
    m.write(DESCRIPTOR + 0x12, 'H', table)
    before = bytes(m.u.mem_read(DESCRIPTOR, 1024))
    assert m.call(0xdd6a08, OBJECT, DESCRIPTOR, JOB) == 1
    descriptor = bytes(m.u.mem_read(DESCRIPTOR, 1024))
    action_offset = (table + 0x1a5) & 0xffff
    stream = b'\x80' + struct.pack('<QI', fence, value) + b'\x00'
    expected = bytearray(before)
    struct.pack_into('<HH', expected, table, action_offset, 14)
    expected[action_offset:action_offset + 14] = stream
    assert descriptor == expected  # Includes absence of unrelated writes.
    assert descriptor[15] == 0  # The caller, not this helper, sets the count.
    return descriptor, stream, action_offset


def produce_job(pe, *, last_sequence, os_fence, node=1, engine=0, dispatch_status=0):
    """Execute complete dd28f8 with an empty, nonpaging context submission."""
    m = Machine(pe)
    m.u.mem_map(BASE + 0xdd3000, 0x1000)
    m.u.mem_write(BASE + 0xdd3000, pe.get_data(0xdd3000, 0x1000))
    assert struct.unpack('<Q', pe.get_data(0xe9c930, 8))[0] == BASE + 0xdd5ab0
    m.write(OBJECT, 'Q', OWNER + 0x8000)
    m.write(OWNER + 0x8000 + 0x378, 'Q', STUB + 4)
    m.write(OWNER + 0x8000 + 0x260, 'Q', STUB + 8)
    m.write(PARENT, 'Q', OWNER + 0x9000)
    m.write(OWNER + 0x9000 + 0x20, 'Q', STUB + 12)
    m.write(DESCRIPTOR, 'Q', OWNER + 0x4000)
    m.write(DESCRIPTOR + 0x3c, 'I', os_fence)
    m.write(DESCRIPTOR + 0x50, 'Q', 0x1234567800)
    m.write(OWNER + 0x4000, 'Q', OWNER + 0x5000)
    m.write(OWNER + 0x4000 + 0x70, 'I', 2)
    m.write(OWNER + 0x4000 + 0x78, 'Q', OWNER + 0x6000)
    m.write(OWNER + 0x4000 + 0x80, 'I', node)
    m.write(OWNER + 0x5000 + 0x38, 'Q', STUB + 16)
    m.write(OBJECT + 0xee0, 'Q', OWNER + 0x7000)
    m.write(OWNER + 0x7000 + 0x10, 'I', 4)
    m.write(OWNER + 0x7000 + node * 0x170 + 0x18, 'I', 0x33)
    m.write(BASE + 0xe3e0c8, 'Q', STUB + 20)
    m.write(BASE + 0xe3e0d0, 'Q', STUB + 24)
    queue = node * 8 + engine
    m.write(PARENT + 0x7d0, 'I', 32)
    m.write(PARENT + 0x7d8 + queue * 4, 'I', 32)
    m.write(PARENT + 0xf98, 'I', 0x5678)
    m.write(PARENT + 0xf9c + queue * 4, 'I', last_sequence)
    m.write(PARENT + (queue + 1) * 8, 'Q', JOB)
    m.u.mem_write(JOB, b'\xa5' * (32 * 0x1f0))
    sequence = (last_sequence + 1) & 0xffffffff
    record = JOB + (sequence % 32) * 0x1f0
    observed = []

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            assert m.reg(0) == OBJECT
            m.ret(PARENT)
        elif pc == STUB + 8:
            assert m.reg(0) == OBJECT
            m.ret(4)
        elif pc == STUB + 12:
            assert [m.reg(i) for i in (0, 1, 2)] == [PARENT, DESCRIPTOR, record]
            observed.append(bytes(m.u.mem_read(record, 0x1f0)))
            m.ret(dispatch_status)
        elif pc == STUB + 16:
            assert m.reg(0) == OWNER + 0x4000
            m.ret(engine)
        elif pc == STUB + 20:
            assert m.reg(0) == OBJECT + 0x58 + node * 8
            m.ret(2)
        elif pc == STUB + 24:
            assert [m.reg(i) for i in (0, 1)] == [OBJECT + 0x58 + node * 8, 2]
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    status = m.call(0xdd28f8, OBJECT, DESCRIPTOR)
    assert status == dispatch_status and len(observed) == 1
    data = observed[0]
    assert struct.unpack_from('<QQQ', data) == (OBJECT, OWNER + 0x6000, OWNER + 0x4000)
    assert struct.unpack_from('<IIIIII', data, 0x20) == (node, engine, 0x5679, sequence, os_fence, 0)
    assert m.read(PARENT + 0xf9c + queue * 4, 'I') == (sequence,)
    assert m.read(PARENT + 0xf98, 'I') == (0x5679,)
    flags = m.read(record + 0x1e8, 'I')[0]
    assert flags == (0x18 if dispatch_status & 0x80000000 else 0)
    return {'sequence': sequence, 'os_fence': os_fence, 'slot': sequence % 32,
            'node': node, 'engine': engine, 'status': status, 'record_flags': flags,
            'global_sequence': 0x5679}


def preactions(pe, sequence, disabled=False):
    """Execute original three-action producer with distinct semaphore banks."""
    m = Machine(pe)
    for address, pointer in ((OBJECT + 0x20, PARENT), (PARENT + 0x6ec0, PRIMARY),
                             (PRIMARY + 0xed8, OWNER), (OWNER + 0x18, OWNER + 0x1000),
                             (OWNER + 0x1000 + 0x4e0, OWNER + 0x2000),
                             (PRIMARY, OWNER + 0x4000),
                             (OWNER + 0x4000 + 0x268, STUB + 4)):
        m.write(address, 'Q', pointer)
    m.write(OWNER + 0x3000 + 4, 'I', 4 if disabled else 0)
    m.write(OWNER + 0x2000 + 0xf00, 'I', 4)
    m.write(OWNER + 0x2000 + 0xefc, 'I', 8)
    m.write(OWNER + 0x1000 + 0x4e8, 'Q', HOST)
    for offset, index in ((0x508, 0x10), (0x52c, 0x100), (0x520, 0x200)):
        m.write(OWNER + 0x1000 + offset, 'III', index, 256, 4)
    m.write(JOB + 0x20, 'I', 2)
    m.write(JOB + 0x2c, 'I', sequence)
    m.write(DESCRIPTOR + 0x10, 'HH', 60, 64)
    before = bytes(m.u.mem_read(DESCRIPTOR, 1024))

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            assert [m.reg(i) for i in (0, 1)] == [PRIMARY, 2]
            m.ret(OWNER + 0x3000)

    m.u.hook_add(UC_HOOK_CODE, hook)
    assert m.call(0xdd6ab0, OBJECT, DESCRIPTOR, JOB) == (0 if disabled else 1)
    data = bytes(m.u.mem_read(DESCRIPTOR, 1024))
    if disabled:
        assert data == before
        return b'', {}
    done, started, control = HOST + 0x60, HOST + 0x440, HOST + 0x840
    stream = (b'\x92' + struct.pack('<QI', done, (sequence - 1) & 0xffffffff) +
              b'\x80' + struct.pack('<QI', started, sequence) +
              b'\x90' + struct.pack('<QI', control, 0xfeed) + b'\x00')
    expected = bytearray(before)
    struct.pack_into('<HH', expected, 60, 68, 40)
    expected[68:108] = stream
    assert data == expected
    return stream, {'done': done, 'started': started, 'control': control}


def control_gate(pe, blocked):
    """Run original gate writer and lock/event wrappers; model only OS APIs."""
    m = Machine(pe)
    for page in (0xe17000, 0xe18000):
        m.u.mem_map(BASE + page, 0x1000)
        m.u.mem_write(BASE + page, pe.get_data(page, 0x1000))
    for address, pointer in ((PARENT + 0x6ec0, PRIMARY), (PRIMARY + 0xed8, OWNER),
                             (OWNER + 0x18, OWNER + 0x1000),
                             (OWNER + 0x1000 + 0x4e0, OWNER + 0x2000),
                             (OWNER + 0x1000 + 0x4f0, DESCRIPTOR)):
        m.write(address, 'Q', pointer)
    m.write(OWNER + 0x2000 + 0xefc, 'I', 8)
    m.write(OWNER + 0x1000 + 0x520, 'III', 0x200, 256, 4)
    m.write(BASE + 0xe3e0c8, 'Q', STUB + 4)
    m.write(BASE + 0xe3e0d0, 'Q', STUB + 8)
    m.write(BASE + 0xe3e0b8, 'Q', STUB + 12)
    events = []
    gate = 0xf00d if blocked & 0xff else 0xfeed

    def hook(_, pc, size, user):
        if pc == STUB + 4:
            assert m.reg(0) == PARENT + 0x7f10
            events.append('lock')
            m.ret(2)
        elif pc == STUB + 8:
            assert [m.reg(i) for i in (0, 1)] == [PARENT + 0x7f10, 2]
            assert m.read(DESCRIPTOR + 0x840, 'I') == (gate,)
            events.append('unlock_after_gate_write')
            m.ret(0)
        elif pc == STUB + 12:
            assert [m.reg(i) for i in (1, 2)] == [0, 0]
            events.append(['set_event', m.reg(0) - PARENT])
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    m.call(0xe18220, PARENT, 2, 0, blocked)
    expected = ['lock', 'unlock_after_gate_write']
    if blocked & 0xff:
        expected += [['set_event', 0x8630], ['set_event', 0x8648]]
    assert events == expected
    return {'control_word': m.read(DESCRIPTOR + 0x840, 'I')[0], 'events': events}


def main():
    path = ROOT / 'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    firmware = (ROOT / 'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
    imports = {entry.address - BASE: entry.name for dll in pe.DIRECTORY_ENTRY_IMPORT for entry in dll.imports}
    assert imports[0xe3e0b8] == b'KeSetEvent'
    assert imports[0xe3e0c8] == b'KeAcquireSpinLockRaiseToDpc'
    assert imports[0xe3e0d0] == b'KeReleaseSpinLock'
    with tempfile.TemporaryDirectory(prefix='n1x-kmd-task-') as tmp:
        library = Path(tmp) / 'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I' + str(ROOT / 'include'), str(ROOT / 'driver/n1x_dla_task.c'),
            '-o', str(library)], check=True)
        native = ctypes.CDLL(str(library)).n1x_dla_build_fenced_task_v2
    native.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Args),
                       ctypes.c_uint64, ctypes.c_uint32]
    native.restype = ctypes.c_int

    def compare_native(stream, fence, value):
        out = ctypes.create_string_buffer(256)
        args = Args(0, HOST + 256, 1000000, 1)
        assert native(out, len(out), ctypes.byref(args), fence, value) == 0
        table = struct.unpack_from('<H', out.raw, 18)[0]
        offset, length = struct.unpack_from('<HH', out.raw, table)
        assert out.raw[15] == 1 and length == 14
        assert out.raw[offset:offset + length] == stream

    rows = []
    for mode in (1, 2):
        for sequence in (0, 1, 0xffffffff, 0x1234567800000001):
            for send_error in (0, 0x65):
                result = submit(pe, mode=mode, sequence=sequence, send_error=send_error)
                assert result['return_value'] == 1 and result['outstanding'] == 1
                assert result['events'] == [
                    ['method_sender', 0x207, 0x12345678, send_error],
                    'original_pending_record_cleanup', ['handler_status', 0]]
                rows.append({'case': 'direct_submit', 'mode': mode, 'sequence': sequence,
                             'injected_sender_error': send_error, **result})
    for gate in (1, 2):
        result = submit(pe, gate=gate)
        assert result == {'return_value': 0, 'envelopes': [], 'events': [], 'outstanding': 0}
        rows.append({'case': 'global_gate', 'gate': gate, **result})
    result = submit(pe, missing_primary=True)
    assert result['return_value'] == 0 and result['outstanding'] == 0
    assert result['events'] == [['handler_status', 0x40]]
    rows.append({'case': 'missing_primary', **result})
    # Windows truncates to 32 address bits after >>8; this is not a Linux policy.
    for address in (0x12345678ff, 0x11234567800):
        result = submit(pe, descriptor=address)
        assert result['events'][0] == ['method_sender', 0x207, 0x12345678, 0]
        rows.append({'case': 'address_encoding', 'address': hex(address), **result})

    for mode in (1, 2):
        for sequence in (0, 1, 31, 32, 33, 0xffffffff):
            result = submit(pe, mode=mode, sequence=sequence, high_level=True)
            expected = 0x1234567800 + (sequence & 31) * 0x800
            assert result['return_value'] == 0 and result['outstanding'] == 1
            assert result['envelopes'][0]['descriptor'] == hex(expected)
            assert result['events'] == [
                ['method_sender', 0x207, expected >> 8, 0],
                'original_pending_record_cleanup', ['handler_status', 0]]
            rows.append({'case': 'original_ring_submit', 'mode': mode, 'sequence': sequence, **result})

    for last_sequence in (0, 31, 0x7fffffff, 0xffffffff):
        for os_fence in (0, 0xfedcba98, 0x80000000):
            for node, engine in ((0, 0), (2, 1)):
                for dispatch_status in (0, 0xc0000001):
                    result = produce_job(pe, last_sequence=last_sequence, os_fence=os_fence,
                        node=node, engine=engine, dispatch_status=dispatch_status)
                    rows.append({'case': 'original_job_producer', 'last_sequence': last_sequence, **result})

    for sequence in (0, 1, 0x80000000, 0xffffffff):
        stream, addresses = preactions(pe, sequence)
        for distance, control in ((1, 0xfeed), (0, 0xfeed), (2, 0xfeed), (1, 0xf00d)):
            prior_complete = distance <= 1
            done = (sequence - distance) & 0xffffffff
            words = {addresses['done']: done, addresses['started']: 0xdeadbeef,
                     addresses['control']: control}
            result = run_postactions(firmware, stream, offset=68, preaction=True, initial_words=words)
            success = prior_complete and control == 0xfeed
            assert result['returned'] and result['status'] == (0 if success else -13)
            assert result['final_words'] == {
                hex(addresses['done']): done, hex(addresses['control']): control,
                hex(addresses['started']): sequence if prior_complete else 0xdeadbeef}
            assert [t['direction'] for t in result['transfers']] == (
                ['read', 'read', 'write', 'read'] if prior_complete else ['read', 'read'])
            post = None
            if success:
                _, poststream, _ = postaction(pe, index=0, multiplier=1, base_index=0,
                    stride=4, value=sequence, fence=addresses['done'])
                post = run_postactions(firmware, poststream,
                    initial_words={int(a, 16): v for a, v in result['final_words'].items()})
                assert post['returned'] and post['status'] == 0
                assert post['final_words'] == {hex(addresses['done']): sequence,
                    hex(addresses['started']): sequence, hex(addresses['control']): control}
            rows.append({'case': 'vendor_preconditions_to_firmware', 'sequence': sequence,
                         'prior_complete': prior_complete, 'completed_distance': distance, 'control': control,
                         'stream': stream.hex(), 'preactions': result, 'postactions': post})
    assert preactions(pe, 1, disabled=True) == (b'', {})
    rows.append({'case': 'engine_property_disables_preactions'})
    for blocked in (0, 1, 2, 0x100):
        rows.append({'case': 'original_control_gate', 'argument': blocked, **control_gate(pe, blocked)})

    for index, multiplier, base_index, stride in ((0, 1, 0, 4), (3, 2, 7, 64), (7, 3, 5, 12)):
        for value in (0, 1, 0x80000000, 0xffffffff):
            descriptor, stream, offset = postaction(pe, index=index, multiplier=multiplier,
                base_index=base_index, stride=stride, value=value)
            compare_native(stream, HOST, value)
            # Real caller dd75ac sets this byte after dd6a08 returns true.
            descriptor = bytearray(descriptor)
            descriptor[15] = 1
            for task_status in (0, -1):
                result = run_postactions(firmware, stream, offset=offset,
                                         descriptor=descriptor, task_status=task_status)
                # A failed task skips postactions and this helper returns zero;
                # its return alone is not the overall task status.
                assert result['returned'] and result['status'] == 0
                assert result['host_word'] == (value if task_status == 0 else 0xdeadbeef)
                if task_status:
                    assert not result['transfers']
                else:
                    assert result['transfers'][-1]['direction'] == 'write'
                    assert result['transfers'][-1]['length'] == 4
                rows.append({'case': 'vendor_postaction_to_firmware', 'index': index,
                             'multiplier': multiplier, 'base_index': base_index, 'stride': stride,
                             'value': value, 'task_status': task_status, 'stream': stream.hex(), **result})
    descriptor, stream, offset = postaction(pe, index=2, multiplier=1, base_index=1,
        stride=8, value=0xfedcba98, table=60, fence=0x12345678900)
    compare_native(stream, 0x12345678900, 0xfedcba98)
    rows.append({'case': '64bit_fence_address', 'stream': stream.hex(), 'offset': offset})
    print(json.dumps({'passed': True, 'hardware_access': False, 'case_count': len(rows),
        'kmd_sha256': KMD_SHA256, 'firmware_sha256': PROFILE1_HASH, 'cases': rows,
        'limitations': [
            'Generic RM object lookup is replaced by a checked command-table dispatch',
            'OS locks, timestamps, logging and low-level method sender are synthetic',
            'Only HAL1 direct submission with parent+0x4a8=0 and empty pending queue is executed',
            'Higher submitter device lookup and descriptor preparation are synthetic callbacks',
            'Job producer uses an empty nonpaging submission; final dispatch and OS locks are synthetic',
            'Preaction engine-property lookup is synthetic; control-gate OS lock/event imports are synthetic',
            'Action streams execute separately, not as a complete inference or scheduler retirement path',
            'Original postaction producer uses valid indices; complete descriptor builder is not executed',
            'RV32 consumer has RAM-only DMA; no scheduler, engine arithmetic or physical DMA ordering',
            'No WDDM scheduler completion, safe buffer reclamation or reset is established',
            'Ignored vendor sender errors and address truncation must not become Linux policy']}, indent=2))


if __name__ == '__main__':
    main()
