#!/usr/bin/env python3
"""Compare Linux HFRP rings against original ARM64 producer/consumer code."""
import ctypes
import errno
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
from test_core_control import ROOT, IO, READ, WRITE, DELAY

BASE = 0x140000000
CTX, DATA, OUT, STACK, STUB = (0x36000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


class Tx(ctypes.Structure):
    _fields_ = [('header', ctypes.c_uint32), ('head_publish_attempted', ctypes.c_uint8),
                ('head_published', ctypes.c_uint8), ('doorbell_attempted', ctypes.c_uint8),
                ('doorbell_written', ctypes.c_uint8)]


class Reply(ctypes.Structure):
    _fields_ = [('header', ctypes.c_uint32), ('sequence', ctypes.c_uint16),
                ('status', ctypes.c_uint16), ('length', ctypes.c_uint8),
                ('payload', ctypes.c_uint8 * 50), ('tail_publish_attempted', ctypes.c_uint8),
                ('tail_published', ctypes.c_uint8)]


def image(head, tail, rx=False, packet=b''):
    ram = bytearray(b'\xa5'*0x200)
    struct.pack_into('<I', ram, 0x110, 0xbbaa0000 | ((tail << 8) | 27 if rx else (27 << 8) | head))
    struct.pack_into('<I', ram, 0x188, 0xddcc0000 | ((27 << 8) | head if rx else (tail << 8) | 27))
    if rx:
        for i, byte in enumerate(packet):
            ram[0x18c+(tail+i)%116] = byte
    return ram


def windows(pe, initial, header, payload=b'', rx=False, handshake=False,
            handshake_fault=None, handshake_status=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd4a000, 0x2000), (0xd57000, 0x1000), (0xcd000, 0x2000),
                      (0xe3d000, 0x2000), (0x13ac000, 0x1000),
                      (0xd3d000, 0x1000), (0xd8000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, DATA, OUT, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(CTX+8, struct.pack('<QQ', STUB+0x10, STUB+0x20))
    u.mem_write(CTX, struct.pack('<Q', BASE+0xd3d750))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))
    ram, trace = bytearray(initial), []
    packets, delays = [], []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == STUB:
            u.reg_write(regs.UC_ARM64_REG_PC, reg(30))
        elif address == STUB+0x10:
            assert reg(0) == CTX and reg(1)%4 == 0 and reg(1) < len(ram)
            value = struct.unpack_from('<I', ram, reg(1))[0]
            trace.append(('read', reg(1), value))
            ret(value)
        elif address == STUB+0x20:
            assert reg(0) == CTX and reg(1)%4 == 0 and reg(1) < len(ram)
            trace.append(('write', reg(1), reg(2)))
            struct.pack_into('<I', ram, reg(1), reg(2))
            if handshake and reg(1) == 0x100:
                if reg(2) == 1:
                    struct.pack_into('<I', ram, 0x110, 0)
                    struct.pack_into('<I', ram, 0x188, 0)
                    struct.pack_into('<I', ram, 0x100, int(handshake_fault == 'reset_stuck'))
                    struct.pack_into('<I', ram, 0x104, 0 if handshake_fault == 'no_ready' else 2)
                elif reg(2) == 2:
                    head = ram[0x110]
                    packets.append(bytes(ram[0x114:0x114+head]))
                    assert packets[-1] == bytes.fromhex('090104000000ccdd00')
                    if handshake_fault != 'no_reply':
                        reply = struct.pack('<I', 8 | (1 << 8) | (handshake_status << 18))+b'\x01\x02\x03\x04'
                        ram[0x18c:0x194] = reply
                        struct.pack_into('<I', ram, 0x188, 8 | (head << 8))
                        struct.pack_into('<I', ram, 0x104, 2)
            if handshake and reg(1) == 0x10c:
                value = struct.unpack_from('<I', ram, 0x104)[0]
                struct.pack_into('<I', ram, 0x104, value & ~reg(2))
            ret()
        elif address == BASE+0xcd148:
            u.mem_write(reg(0), bytes(reg(2)))
            ret()
        elif address == BASE+0xe3d030:
            u.mem_write(reg(0), bytes(u.mem_read(reg(1), reg(2))))
            ret(reg(0))
        elif address == BASE+0xce4b0:
            ret()
        elif address == BASE+0xd82c0:
            assert reg(0) == 10
            delays.append(reg(0))
            ret()

    u.hook_add(UC_HOOK_CODE, hook)

    def call(rva, args):
        for i, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        try:
            u.emu_start(BASE+rva, STOP, count=2000000)
        except Exception as exc:
            raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
        assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return reg(0)

    call(0xd4aeb8, (CTX, 0))
    if handshake:
        status = call(0xd4af88, (CTX,))
        response, firmware_status, outstanding = b'', 0, 0
    elif rx:
        sequence = (header >> 8) & 0x3ff
        if sequence != 0x3ff:
            u.mem_write(CTX+0xd0+sequence*8, struct.pack('<Q', OUT))
            u.mem_write(CTX+0x20d0+sequence*8, struct.pack('<Q', OUT+0x100))
            u.mem_write(CTX+0x50+(sequence//32)*4, struct.pack('<I', 1 << (sequence%32)))
        status = call(0xd4b7f0, (CTX,))
        response = bytes(u.mem_read(OUT, len(payload)))
        firmware_status = int.from_bytes(u.mem_read(OUT+0x100, 2), 'little')
        outstanding = int.from_bytes(u.mem_read(CTX+0x50+(sequence//32)*4, 4), 'little')
    else:
        if payload:
            u.mem_write(DATA, payload)
        status = call(0xd4b3a0, (CTX, header, DATA, len(payload), 0))
        response, firmware_status, outstanding = b'', 0, 0
    return {'status': status, 'ram': ram, 'trace': trace, 'payload': response,
            'firmware_status': firmware_status, 'outstanding': outstanding,
            'handshake_packets': [p.hex() for p in packets], 'delay_count': len(delays)}


def linux(lib, initial, *, payload=b'', command=400, sequence=1, rx=False,
          fail_at=None, fail_after_write=False):
    ram, trace = bytearray(initial), []
    invalid_accesses = []

    @READ
    def read(_, offset, pointer):
        if offset%4 != 0 or offset+4 > len(ram):
            invalid_accesses.append(('read', offset))
            return -errno.EFAULT
        value = struct.unpack_from('<I', ram, offset)[0]
        trace.append(('read', offset, value))
        if len(trace)-1 == fail_at:
            return -errno.EIO
        pointer[0] = value
        return 0

    @WRITE
    def write(_, offset, value):
        if offset%4 != 0 or offset+4 > len(ram):
            invalid_accesses.append(('write', offset))
            return -errno.EFAULT
        trace.append(('write', offset, value))
        failed = len(trace)-1 == fail_at
        if not failed or fail_after_write:
            struct.pack_into('<I', ram, offset, value)
        return -errno.EIO if failed else 0

    io = IO(None, write, read, DELAY())
    result = Reply() if rx else Tx()
    data = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
    code = (lib.n1x_hfrp_read_reply(ctypes.byref(io), ctypes.byref(result)) if rx else
            lib.n1x_hfrp_queue_command(ctypes.byref(io), command, sequence, data, len(payload), ctypes.byref(result)))
    assert not invalid_accesses, invalid_accesses
    return code, result, ram, trace


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-ring-') as tmp:
        library = Path(tmp)/'hfrp.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_hfrp.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    lib.n1x_hfrp_queue_command.argtypes = [ctypes.POINTER(IO), ctypes.c_uint16, ctypes.c_uint16,
                                          ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32, ctypes.POINTER(Tx)]
    lib.n1x_hfrp_read_reply.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Reply)]
    tx_cases, rx_cases = 0, 0
    handshake_cases = []
    for fault, firmware_status, expected, count in (
            (None, 0, 0, 0), (None, 0x123, 0, 0),
            ('reset_stuck', 0, 0, 5000), ('no_ready', 0, 0x65, 5000),
            ('no_reply', 0, 0x65, 5000)):
        original = windows(pe, image(0, 0), 0, handshake=True,
                           handshake_fault=fault, handshake_status=firmware_status)
        assert original['status'] == expected and original['delay_count'] == count
        assert len(original['handshake_packets']) == int(fault != 'no_ready')
        handshake_cases.append({'fault': fault, 'firmware_status': firmware_status,
                                 'status': original['status'], 'delay_count': count,
                                 'packets': original['handshake_packets']})
    for head in (0, 1, 2, 3, 64, 112, 113, 114, 115):
        for length in (0, 1, 3, 5, 32, 50, 111):
            payload = bytes((i*7+3)&255 for i in range(length))
            initial = image(head, head)
            header = (length+4) | (1 << 8) | (400 << 18)
            original = windows(pe, initial, header, payload)
            code, result, ram, trace = linux(lib, initial, payload=payload)
            assert code == original['status'] == 0
            assert result.header == header and result.head_published and result.doorbell_written
            # The original ring producer does not ring the doorbell; its parent does.
            original['ram'][0x100:0x104] = struct.pack('<I', 2)
            assert ram == original['ram'], (head, length)
            assert trace[-2][0:2] == ('write', 0x110) and trace[-1] == ('write', 0x100, 2)
            tx_cases += 1
    for head in (0, 113):
        for used in (111, 112, 115):
            initial = image(head, (head-used)%116)
            original = windows(pe, initial, 0x6400104)
            code, result, ram, trace = linux(lib, initial)
            assert code == (0 if used == 111 else -errno.ENOSPC)
            assert original['status'] == (0 if used == 111 else 0xffff)
            if code:
                assert ram == initial and not any(e[0] == 'write' for e in trace)
            tx_cases += 1
    for command, sequence in ((0, 0), (999, 1022), (1023, 512)):
        initial = image(114, 114)
        header = 4 | (sequence << 8) | (command << 18)
        original = windows(pe, initial, header)
        code, result, ram, trace = linux(lib, initial, command=command, sequence=sequence)
        original['ram'][0x100:0x104] = struct.pack('<I', 2)
        assert code == 0 and ram == original['ram'] and result.header == header
        tx_cases += 1
    for tail in (0, 1, 2, 3, 64, 112, 113, 114, 115):
        for length in (0, 1, 3, 5, 32, 50):
            for sequence, status in ((1, 0), (1022, 0x3ff), (1023, 0x42)):
                payload = bytes((i*11+5)&255 for i in range(length))
                header = (length+4) | (sequence << 8) | (status << 18)
                initial = image((tail+length+4)%116, tail, True, struct.pack('<I', header)+payload)
                original = windows(pe, initial, header, payload, True)
                code, result, ram, trace = linux(lib, initial, rx=True)
                assert code == 0 and result.tail_published and result.header == header
                assert (result.sequence, result.status, result.length) == (sequence, status, length)
                assert bytes(result.payload[:length]) == payload and ram == original['ram']
                if sequence != 1023:
                    assert original['payload'] == payload and original['firmware_status'] == status
                    assert original['outstanding'] == 0
                rx_cases += 1
    # Consume only one packet per call, including across the physical ring end.
    first = struct.pack('<I', 5 | (2 << 8) | (3 << 18))+b'\xaa'
    second = struct.pack('<I', 7 | (3 << 8))+b'\xbb\xcc\xdd'
    initial = image(9, 113, True, first+second)
    code, result, ram, trace = linux(lib, initial, rx=True)
    assert code == 0 and result.sequence == 2 and result.status == 3 and result.payload[0] == 0xaa
    assert ram[0x111] == 2 and ram[0x110] == initial[0x110]
    code, result, ram, trace = linux(lib, ram, rx=True)
    assert code == 0 and result.sequence == 3 and bytes(result.payload[:3]) == b'\xbb\xcc\xdd'
    assert ram[0x111] == 9
    assert linux(lib, ram, rx=True)[0] == -errno.EAGAIN
    # Malformed/incomplete frames are not handed to the unsafe original consumer.
    malformed = [(0, 0, 0, -errno.EAGAIN), (2, 0, 2, -errno.EAGAIN),
                 (4, 0, 0, -errno.EPROTO), (4, 0, 3, -errno.EPROTO),
                 (4, 0, 5, -errno.EAGAIN), (55, 0, 55, -errno.EMSGSIZE),
                 (116, 0, 4, -errno.EPROTO), (0, 116, 4, -errno.EPROTO)]
    for head, tail, total, expected in malformed:
        initial = image(head, tail, True, struct.pack('<I', total))
        code, result, ram, trace = linux(lib, initial, rx=True)
        assert code == expected and ram == initial and not result.tail_publish_attempted
    fault_cases = 0
    for rx in (False, True):
        payload = b'\x08\x07\x06\x05\x04'
        initial = image(9, 0, True, struct.pack('<I', 9 | (1 << 8))+payload) if rx else image(113, 113)
        baseline = linux(lib, initial, payload=payload, rx=rx)
        assert baseline[0] == 0
        for index, operation in enumerate(baseline[3]):
            for after in (False, True) if operation[0] == 'write' else (False,):
                code, result, ram, trace = linux(lib, initial, payload=payload, rx=rx,
                                                fail_at=index, fail_after_write=after)
                assert code == -errno.EIO and len(trace) == index+1
                assert trace == baseline[3][:index+1]
                if rx:
                    assert not result.tail_published
                    assert result.tail_publish_attempted == (operation[0:2] == ('write', 0x110))
                else:
                    assert not result.doorbell_written
                    if operation[0:2] == ('write', 0x110):
                        assert result.head_publish_attempted and not result.head_published
                    if operation[0:2] == ('write', 0x100):
                        assert result.head_published and result.doorbell_attempted
                fault_cases += 1
    for head, tail in ((116, 0), (0, 116), (255, 255)):
        initial = image(head, tail)
        code, result, ram, trace = linux(lib, initial)
        assert code == -errno.EPROTO and ram == initial
    # Invalid arguments must not call I/O or modify an existing output object.
    for rx in (False, True):
        result = Reply() if rx else Tx()
        for missing in ('io', 'read', 'write', 'result'):
            io = IO(None, WRITE(), READ(), DELAY())
            unexpected = []
            @READ
            def unexpected_read(_, offset, out):
                unexpected.append('read')
                return -errno.EIO
            @WRITE
            def unexpected_write(_, offset, value):
                unexpected.append('write')
                return -errno.EIO
            if missing != 'read': io.read32 = unexpected_read
            if missing != 'write': io.write32 = unexpected_write
            ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
            before = bytes(result)
            args = (None if missing == 'io' else ctypes.byref(io),
                    None if missing == 'result' else ctypes.byref(result))
            code = (lib.n1x_hfrp_read_reply(*args) if rx else
                    lib.n1x_hfrp_queue_command(args[0], 400, 1, None, 0, args[1]))
            assert code == -errno.EINVAL and bytes(result) == before and not unexpected
    for command, sequence, length in ((1024, 1, 0), (1, 1023, 0), (1, 1, 112), (1, 1, 5)):
        result = Tx()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        io = IO(None, unexpected_write, unexpected_read, DELAY())
        unexpected.clear()
        assert lib.n1x_hfrp_queue_command(ctypes.byref(io), command, sequence, None,
                                          length, ctypes.byref(result)) == -errno.EINVAL
        assert bytes(result) == before and not unexpected
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'tx_windows_comparisons': tx_cases, 'rx_windows_comparisons': rx_cases,
                      'fault_cases': fault_cases, 'malformed_cases': len(malformed),
                      'original_handshake_cases': handshake_cases,
                      'limitations': ['Mode-0 layout only; no physical MMIO or live handshake',
                                      'Original initializer, handshake, poll, ring producer/consumer and byte helpers run; register/OS boundaries are stubs',
                                      'No pending-command manager, timeout recovery, power transition or inference']}, indent=2))


if __name__ == '__main__':
    main()
