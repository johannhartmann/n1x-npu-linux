#!/usr/bin/env python3
"""Reconstruct original reinit parser/builder and test Linux words/exchange."""
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
from unicorn import arm64_const as regs
from unicorn import UC_HOOK_CODE
from extract_firmware import KMD_SHA256
from emulate_sroot_image import machine, call, BASE, INPUT, BUFFER, STACK, CTX
from test_sroot_send import ROOT, HASH
from test_sroot_exchange import Bus, windows
from test_core_control import IO, READ, WRITE, DELAY
from test_sroot_receive import Reply


def original(pe, request):
    u = machine(pe)
    u.mem_write(INPUT, request)
    status = call(u, 0x37f0, (INPUT, len(request), BUFFER))
    if status:
        return status, None
    tag, mode = struct.unpack('<II', u.mem_read(BUFFER, 8))
    frame = STACK+0x4000
    u.mem_write(frame+0x18, struct.pack('<II', tag, mode))
    for i, value in ((19, BASE+0x1b358), (20, CTX), (26, frame)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.emu_start(BASE+0x31a34, BASE+0x31a88, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x31a88
    raw = bytes(u.mem_read(u.reg_read(regs.UC_ARM64_REG_X0), 72))
    assert raw[8] == 0x1a and raw[0x18] == 3
    payload = struct.unpack_from('<Q', raw, 0x10)[0]
    words = list(struct.unpack('<III', u.mem_read(payload, 12)))
    assert words == [tag, 0, mode]
    return 0, words


def selection(kmd, force_upload):
    # Separate KMD slice; original callback-slot selection, synthetic HAL pointers.
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from emulate_sroot_image import STUB
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(BASE+0xd47000, 0x1000)
    u.mem_write(BASE+0xd47000, kmd.get_data(0xd47000, 0x1000))
    u.mem_map(BASE+0xe3e000, 0x1000)
    for address in (CTX, BUFFER, STUB):
        u.mem_map(address, 0x1000)
    for offset in (0x70, 0x80, 0x90):
        u.mem_write(STUB+offset, bytes.fromhex('c0035fd6'))
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB+0x70))
    u.mem_write(CTX+0x400, struct.pack('<QQ', STUB+0x90, STUB+0x80))
    u.mem_write(CTX+0x460, struct.pack('<I', 2))
    u.mem_write(CTX+0x486, bytes([force_upload]))
    u.reg_write(regs.UC_ARM64_REG_X21, CTX)
    u.reg_write(regs.UC_ARM64_REG_X22, BUFFER)
    calls = []
    def hook(_, address, size, user):
        if address in (STUB+0x80, STUB+0x90):
            args = [u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(5)]
            assert args[:2] == [BUFFER, CTX]
            if address == STUB+0x90:
                assert args[2:] == [0, 0, 1]
            calls.append('upload_slot_408' if address == STUB+0x80 else 'reinit_slot_400_subtype_1')
            u.emu_stop()
    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(BASE+0xd47460, 0, count=1000)
    assert calls == ['upload_slot_408' if force_upload else 'reinit_slot_400_subtype_1']
    return {'hal_byte_486': force_upload, 'selected': calls[0]}


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    kmd_path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(kmd_path.read_bytes()).hexdigest() == KMD_SHA256
    kmd = pefile.PE(str(kmd_path), fast_load=True)
    selection_cases = [selection(kmd, value) for value in (0, 1, 255)]
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-reinit-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    build = lib.n1x_sroot_build_reinit
    build.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_size_t]
    valid = invalid = 0
    requests = []
    for tag in (0xc0300, 0xc0302, 0xc0303, 0xc0304, 0xc0305, 0x90400):
        requests += [struct.pack('<I', tag), struct.pack('<II', tag, 1), struct.pack('<II', tag, 2)]
    requests += [bytes(n) for n in (0, 1, 3, 5, 7, 9)]
    requests += [struct.pack('<II', tag, 2) for tag in (0, 0xc0301, 0xc0306, 0xffffffff)]
    requests += [struct.pack('<II', 0xc0302, mode) for mode in (0, 3, 0xffffffff)]
    for request in requests:
        status, expected = original(pe, request)
        words = (ctypes.c_uint32*3)(1, 2, 3)
        result = build(words, request, len(request))
        if status:
            assert status == 0xc000000d and result == -errno.EINVAL and list(words) == [1, 2, 3]
            invalid += 1
        else:
            assert result == 0 and list(words) == expected
            valid += 1
    words = (ctypes.c_uint32*3)(1, 2, 3)
    assert build(words, None, 8) == -errno.EINVAL and list(words) == [1, 2, 3]
    assert build(None, struct.pack('<I', 0xc0302), 4) == -errno.EINVAL
    # Exact request sent by nvlddmkm's subtype-1 transport.
    request = bytes.fromhex('02030c0002000000')
    assert build(words, request, 8) == 0 and list(words) == [0xc0302, 0, 2]
    bus_original, bus_linux = Bus(16, 0x1a, []), Bus(16, 0x1a, [])
    assert windows(pe, bus_original, 0x1a, list(words), 0) == (0, 0, [])

    @READ
    def read(_, offset, output):
        output[0] = bus_linux.read(offset)
        return 0

    @WRITE
    def write(_, offset, value):
        bus_linux.write(offset, value)
        return 0

    @DELAY
    def delay(_, interval):
        raise AssertionError('Unexpected delay on ready bus')

    io, reply = IO(None, write, read, delay), Reply()
    exchange = lib.n1x_sroot_exchange
    exchange.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint8,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint8, ctypes.POINTER(Reply),
                         ctypes.c_uint32, ctypes.c_uint32]
    assert exchange(ctypes.byref(io), 16, 0x1a, words, 3, ctypes.byref(reply), 1, 0) == 0
    assert bus_original.trace == bus_linux.trace and bus_linux.tx == [0x31a, 0xc0302, 0, 2]
    print(json.dumps({'passed': True, 'hardware_access': False, 'valid_parser_builder_cases': valid,
                      'invalid_parser_builder_cases': invalid, 'command': '0x1a',
                      'p16_payload_words': list(words), 'complete_exchange_trace_matches': True,
                      'kmd_selection_cases': selection_cases,
                      'limitations': ['Synthetic responses and mailbox readiness',
                                      'No physical reinitialization, DMA quiescence or NPU boot established']}, indent=2))


if __name__ == '__main__':
    main()
