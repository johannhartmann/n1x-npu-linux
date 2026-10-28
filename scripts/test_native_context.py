#!/usr/bin/env python3
"""Native version-2 checkpoint transport against original HAL-1 firmware."""
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

from diagnostic_fixture import build_relu
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from inspect_kmd_preemption_request import build as build_preempt
from inspect_kmd_task_submission import KMD_SHA256
from inspect_kmd_command_descriptor import build as build_windows_task
from test_profile1_context_save import check
from test_task_descriptor import Args


def check_builders(lib, pe):
    """Compare the context header with Windows and check transactional failures."""
    limit, count = 1 << 36, 0
    context = lib.n1x_dla_build_context_header
    task = lib.n1x_dla_build_context_fenced_task_v2
    for address in (256, HOST+0xc000, limit-256):
        out = ctypes.create_string_buffer(b'\xa5'*80, 80)
        assert context(out, 80, address, 7) == 0
        _, _, expected, _ = build_windows_task(pe, context=True, context_address=address)
        assert out.raw == expected+b'\xa5'*16
        count += 1
    for address, size in ((0, 64), (1, 64), (limit, 64), (limit-128, 64), (256, 63)):
        out = ctypes.create_string_buffer(b'\xa5'*80, 80)
        assert context(out, size, address, 0) == -22 and out.raw == b'\xa5'*80
        count += 1
    assert context(None, 64, 256, 0) == -22
    count += 1
    for restore in (0, 1):
        args = Args(0, limit-256, 1800, 8)
        out, base = ctypes.create_string_buffer(b'\xa5'*272, 272), ctypes.create_string_buffer(256)
        assert task(out, 272, ctypes.byref(args), limit-4, 7, limit-256, restore) == 0
        assert lib.n1x_dla_build_fenced_task_v2(base, 256, ctypes.byref(args), limit-4, 7) == 0
        expected = bytearray(base.raw)
        struct.pack_into('<H', expected, 41, 4 | restore << 1)
        struct.pack_into('<Q', expected, 51, limit-256)
        assert out.raw == expected+b'\xa5'*16
        count += 1
    invalid = [dict(next=256), dict(addresses=0), dict(addresses=257), dict(addresses=limit),
               dict(addresses=limit-256, number=33), dict(number=0), dict(number=513),
               dict(fence=0), dict(fence=257), dict(fence=limit), dict(header=0),
               dict(header=257), dict(header=limit), dict(restore=2), dict(size=255)]
    for change in invalid:
        args = Args(change.get('next', 0), change.get('addresses', 256), 1800, change.get('number', 8))
        out = ctypes.create_string_buffer(b'\xa5'*272, 272)
        assert task(out, change.get('size', 272), ctypes.byref(args), change.get('fence', 512),
                    7, change.get('header', 768), change.get('restore', 0)) == -22
        assert out.raw == b'\xa5'*272
        count += 1
    args = Args(0, 256, 1800, 8)
    out = ctypes.create_string_buffer(b'\xa5'*272, 272)
    assert task(out, 272, None, 512, 7, 768, 0) == -22 and out.raw == b'\xa5'*272
    assert task(None, 272, ctypes.byref(args), 512, 7, 768, 0) == -22
    count += 2
    # Public helper promises args may overlap the destination.
    ctypes.memmove(out, ctypes.byref(args), ctypes.sizeof(args))
    assert task(out, 272, ctypes.cast(out, ctypes.POINTER(Args)), 512, 7, 768, 1) == 0
    baseline = ctypes.create_string_buffer(272)
    assert task(baseline, 272, ctypes.byref(args), 512, 7, 768, 1) == 0
    assert out.raw[:256] == baseline.raw[:256] and out.raw[256:] == b'\xa5'*16
    return count+1


def check_preemption_builder(lib, pe):
    build = lib.n1x_dla_build_preempt_request
    cases = 0
    for sequence, submitted, node in ((0, 0, 0), (8, 5, 0), (16, 31, 1),
                                       (24, 32, 2), (32, 0xffffffff, 3),
                                       (0xfffffff8, 0x12345678, 4)):
        reference, header, post, fence = build_preempt(pe, sequence, submitted, node)
        out = ctypes.create_string_buffer(b'\xa5'*528, 528)
        dma = HOST+header
        assert build(out, 528, dma, HOST+fence, submitted, sequence) == 0
        expected = bytearray(512)
        expected[:32] = reference[header:header+32]
        # Only relocate the postaction pointer; compare its original contents.
        struct.pack_into('<Q', expected, 13, dma+256)
        expected[256:283] = reference[post:post+27]
        assert out.raw == expected+b'\xa5'*16
        cases += 1
    limit = 1 << 36
    for dma, fence in ((256, 252), (256, 768), (limit-512, limit-516)):
        out = ctypes.create_string_buffer(512)
        assert build(out, 512, dma, fence, 5, 8) == 0
        assert struct.unpack_from('<Q', out.raw, 13)[0] == dma+256
        assert struct.unpack_from('<Q', out.raw, 257)[0] == fence
        assert struct.unpack_from('<Q', out.raw, 270)[0] == fence
        cases += 1
    invalid = [(0, 1024, 512), (257, 1024, 512), (limit-256, 1024, 512),
               (limit, 1024, 512), (256, 0, 512), (256, 1025, 512),
               (256, limit, 512), (256, 1024, 511)]
    invalid += [(256, 256+offset, 512) for offset in (0, 28, 256, 280, 508)]
    for dma, fence, size in invalid:
        out = ctypes.create_string_buffer(b'\xa5'*528, 528)
        assert build(out, size, dma, fence, 5, 8) == -22
        assert out.raw == b'\xa5'*528
        cases += 1
    assert build(None, 512, 256, 1024, 5, 8) == -22
    return cases+1


def start_native(lib, pe, firmware, model, *, job_flags=0, save_context=True,
                 response_handler=None, enabled=True):
    backing, addresses, layout = build_relu(model, HOST+0x10000)
    payload = bytearray(0x10000)+backing
    descriptor = ctypes.create_string_buffer(256)
    args = Args(0, HOST+0x1000, 1800, len(addresses))
    assert lib.n1x_dla_build_context_fenced_task_v2(
        descriptor, 256, ctypes.byref(args), HOST+0x6060, 1, HOST+0xa000, bool(job_flags)) == 0
    payload[:256] = descriptor.raw
    assert save_context
    header = ctypes.create_string_buffer(64)
    assert lib.n1x_dla_build_context_header(header, 64, HOST+0xc000, 0) == 0
    payload[0xa000:0xa040] = header.raw
    wire = ctypes.create_string_buffer(256)
    array = (ctypes.c_uint64*len(addresses))(*addresses)
    assert lib.n1x_dla_build_address_list(wire, 256, array, len(addresses)) == 0
    payload[0x1000:0x1100] = wire.raw
    request_header, fence = 0xb000, 0xf040
    request = ctypes.create_string_buffer(512)
    assert lib.n1x_dla_build_preempt_request(request, 512, HOST+request_header,
                                          HOST+fence, 5, 8) == 0
    payload[request_header:request_header+512] = request.raw
    struct.pack_into('<I', payload, fence, 0xa5a5a5a5)
    input_data = (ROOT/'results/relu-probe/input-chw16-fp16.bin').read_bytes()
    assert hashlib.sha256(input_data).hexdigest() == '73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45'
    for memory, data in ((1, input_data), (2, b'\xa5'*3136)):
        offset = 0x10000+layout[memory]['offset']
        payload[offset:offset+3136] = data
    live, notifications = [], []

    def word(u, address):
        return int.from_bytes(u.mem_read(address, 4), 'little')

    def submit(u):
        method, data = ((0x31c, int(enabled)), (0x307, HOST >> 8))[len(live)]
        live.append(u)
        u.mem_write(MMIO+0x68, struct.pack('<II', method, data))

    def ack(u):
        notifications.append({'mailbox': list(struct.unpack('<II', u.mem_read(MMIO+0x40, 8))),
                              'state': word(u, 0x1862a4),
                              'outstanding': int.from_bytes(u.mem_read(0x183720, 1), 'little'),
                              'preempt_fence': word(u, HOST+fence), 'task_done': word(u, HOST+0x6060)})
        if response_handler is not None and response_handler(u):
            return
        for offset in (8, 0xd28):
            u.mem_write(MMIO+offset, struct.pack('<I', word(u, MMIO+offset) & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(firmware, host_submit=submit, host_handler=ack, sequence=[0, 0],
                 host_payload=payload, boot_from_reset=True, via_trap=True,
                 dma_register_model=True, resume_scheduler=True)
    u = live[-1]
    assert result['returned'] and result['scheduler_wfi']
    assert result['wfi_return_address'] == 0x101424
    assert result['firmware_code_unmodified'] and not result['function_stubs']
    assert word(u, 0x1862a4) == word(u, 0x180114) == 1
    internal = word(u, word(u, 0x1862bc)+40)
    assert word(u, internal+40) == 1 and word(u, HOST+0x6060) == 0
    return {'u': u, 'result': result, 'word': lambda address: word(u, address),
            'layout': layout, 'input': input_data, 'payload': payload,
            'notifications': notifications, 'header': request_header, 'fence': fence,
            'internal': internal, 'context_header': HOST+0xa000, 'context_buffer': HOST+0xc000,
            'checkpoint_budget': 0}


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-native-context-') as tmp:
        path = Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_task.c'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(path)], check=True)
        lib = ctypes.CDLL(str(path))
    lib.n1x_dla_build_fenced_task_v2.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(Args), ctypes.c_uint64, ctypes.c_uint32]
    lib.n1x_dla_build_context_fenced_task_v2.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(Args), ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint8]
    lib.n1x_dla_build_context_header.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint64, ctypes.c_uint8]
    lib.n1x_dla_build_preempt_request.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32]
    lib.n1x_dla_build_address_list.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint16]
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    builder_cases = check_builders(lib, pe)
    preemption_cases = check_preemption_builder(lib, pe)
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    cases = [check(lib, pe, firmware, model, ready, flags, irq, start=start_native)
             for ready in (0, 1, 15, 16, 17) for flags in (0, 128) for irq in (0x400, 0x800)]
    print(json.dumps({'passed': True, 'hardware_access': False, 'case_count': len(cases),
                      'builder_cases': builder_cases,
                      'preemption_builder_cases': preemption_cases,
                      'cases': cases, 'firmware_sha256': PROFILE1_HASH, 'kmd_sha256': KMD_SHA256,
                      'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'limitations': ['Native C task/address/header/preemption serialization; Windows used only as byte oracle',
                                      'Synthetic engine status, IRQs, RAM DMA and model allocations',
                                      'No arithmetic, engine context transfer or physical quiescence proof',
                                      'Not wired into Linux EXEC or a public cancel/resume API']}, indent=2))


if __name__ == '__main__':
    main()
