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


def main():
    path = ROOT / 'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    firmware = (ROOT / 'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    pe = pefile.PE(str(path), fast_load=True)
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
            'Original postaction producer uses valid indices; complete descriptor builder is not executed',
            'RV32 consumer has RAM-only DMA; no scheduler, engine arithmetic or physical DMA ordering',
            'No WDDM scheduler completion, safe buffer reclamation or reset is established',
            'Ignored vendor sender errors and address truncation must not become Linux policy']}, indent=2))


if __name__ == '__main__':
    main()
