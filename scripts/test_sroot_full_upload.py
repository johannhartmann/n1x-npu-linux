#!/usr/bin/env python3
"""Replay the complete Windows upload handler against the first live reply, offline."""
import ctypes
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ
from unicorn import arm64_const as regs

from extract_firmware import KMD_SHA256
from test_core_control import IO, READ, WRITE, DELAY
from test_sroot_exchange import Bus
from test_sroot_receive import Reply
from test_sroot_send import ROOT, BASE, HASH

# Separate arenas: the real 67508-byte image exceeds the older slice probes'
# 64 KiB allocations. Unmapped gaps prevent silent aliasing into another object.
CTX, DEVICE, REQUEST, MEMORY, INPUT, BUFFER, ADAPTER, OPS, TABLE, STUB, STACK, MMIO = (
    0x28000000+i*0x100000 for i in range(12))
STOP = STUB+0xff0
DMA = 0xfffe0000
LIVE_REPLY = 0x2a006018
FALLBACK_LIVE_REPLY = 0x253ff018
IMAGE_HASH = '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4'


def windows(pe, blob, bus, fill):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    image = pe.get_memory_mapped_image()
    u.mem_map(BASE, (len(image)+4095) & ~4095)
    u.mem_write(BASE, image)
    for address in (CTX, DEVICE, REQUEST, MEMORY, INPUT, BUFFER, ADAPTER,
                    OPS, TABLE, STUB, STACK, MMIO):
        u.mem_map(address, 0x20000)
    u.mem_write(INPUT, blob)
    u.mem_write(BUFFER, bytes([fill])*(len(blob)+16))
    u.mem_write(STACK, b'\xa5'*0x20000)

    def put(address, value):
        u.mem_write(address, struct.pack('<Q', value))

    stub_names = ['cfg', 'context', 'lock', 'timer_start', 'input_memory',
                  'memory_buffer', 'pdo', 'adapter', 'allocate', 'physical',
                  'free', 'timer_stop', 'unlock']
    stubs = {name: STUB+16*i for i, name in enumerate(stub_names)}
    for address in stubs.values():
        u.mem_write(address, bytes.fromhex('c0035fd6'))
    for address, value in ((BASE+0x1b358, BASE+0x1b358), (BASE+0x1b838, TABLE),
                           (BASE+0x19200, stubs['cfg']), (ADAPTER+8, OPS),
                           (BASE+0x191a8, stubs['adapter']),
                           (BASE+0x191a0, stubs['physical']),
                           (OPS+0x110, stubs['allocate']), (OPS+0x18, stubs['free']),
                           (CTX+8, 0x1234), (CTX+0x10, 0x5678), (CTX+0x58, MMIO)):
        put(address, value)
    for offset, name in ((0x650, 'context'), (0x9c8, 'lock'), (0x9f8, 'timer_start'),
                         (0x858, 'input_memory'), (0x610, 'memory_buffer'),
                         (0x108, 'pdo'), (0xa00, 'timer_stop'), (0x9d0, 'unlock')):
        put(TABLE+offset, stubs[name])
    u.mem_write(CTX+0x28, struct.pack('<III', 0xe0000000, 0xffffffff, 0x1fffffff))
    u.mem_write(CTX+0x50, struct.pack('<I', bus.stride))
    calls, reached, captured = [], set(), {}
    original = {0x31c98, 0x314a0, 0x3b08, 0x39b0, 0x154a0,
                0x5580, 0x54c0, 0x5130, 0x5328, 0x4f70, 0x3e00}

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address-BASE in original:
            reached.add(address-BASE)
        if address == BASE+0x5580:
            raw = bytes(u.mem_read(reg(0), 72))
            assert raw[8] == 0x18 and raw[0x18] == 3
            assert struct.unpack_from('<Q', raw, 0x30)[0] == BASE+0x3f80
            pointer = struct.unpack_from('<Q', raw, 0x10)[0]
            captured['words'] = list(struct.unpack('<III', u.mem_read(pointer, 12)))
            captured['staged'] = bytes(u.mem_read(BUFFER, len(blob)))
            captured['status_pointer'] = struct.unpack_from('<Q', raw, 0x40)[0]
        name = next((name for name, ptr in stubs.items() if ptr == address), None)
        if name is None or name == 'cfg':
            return
        calls.append(name)
        result = 0
        if name == 'context':
            assert reg(1) == DEVICE
            result = CTX
        elif name == 'lock':
            assert reg(1) == 0x5678 and reg(2) == 0
        elif name == 'timer_start':
            assert reg(1) == 0x1234 and reg(2) == (-700000000 & ((1 << 64)-1))
        elif name == 'input_memory':
            assert reg(1) == REQUEST
            put(reg(2), MEMORY)
        elif name == 'memory_buffer':
            assert reg(1) == MEMORY
            put(reg(2), len(blob))
            result = INPUT
        elif name == 'pdo':
            assert reg(1) == DEVICE
            result = DEVICE+0x100
        elif name == 'adapter':
            assert reg(0) == DEVICE+0x100
            description = bytes(u.mem_read(reg(1), 64))
            assert struct.unpack_from('<I', description)[0] == 3
            assert struct.unpack_from('<I', description, 32)[0] == len(blob)
            assert struct.unpack_from('<I', description, 40)[0] == 64
            result = ADAPTER
        elif name == 'allocate':
            assert reg(0) == ADAPTER and reg(3) == len(blob)
            assert reg(4) == reg(6) == 0 and reg(7) == CTX+0x80
            assert struct.unpack('<Q', u.mem_read(reg(1), 8))[0] == 0xe0000000
            assert struct.unpack('<Q', u.mem_read(reg(2), 8))[0] == 0xffffffff
            assert struct.unpack('<I', u.mem_read(reg(5), 4))[0] == 0  # MmNonCached
            put(reg(7), DMA)
            result = BUFFER
        elif name == 'physical':
            assert reg(0) == BUFFER
            result = 0x12345678000  # Deliberately distinct from the DMA address.
        elif name == 'free':
            assert [reg(i) for i in range(5)] == [ADAPTER, len(blob), DMA, BUFFER, 0]
        elif name == 'timer_stop':
            assert reg(1) == 0x1234 and reg(2) == 1
        elif name == 'unlock':
            assert reg(1) == 0x5678
        else:
            raise AssertionError(name)
        u.reg_write(regs.UC_ARM64_REG_X0, result)

    def memory(_, access, address, size, value, user):
        assert size == 4
        if access == UC_MEM_READ:
            u.mem_write(address, struct.pack('<I', bus.read(address-MMIO)))
        else:
            bus.write(address-MMIO, value)

    u.hook_add(UC_HOOK_CODE, hook)
    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory, begin=MMIO, end=MMIO+0xfff)
    u.reg_write(regs.UC_ARM64_REG_X0, DEVICE)
    u.reg_write(regs.UC_ARM64_REG_X1, REQUEST)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x10000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x31c98, STOP, count=300000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert u.reg_read(regs.UC_ARM64_REG_SP) == STACK+0x10000
    assert reached == original, [hex(rva) for rva in original-reached]
    assert bytes(u.mem_read(INPUT, len(blob))) == blob
    assert bytes(u.mem_read(BUFFER+len(blob), 16)) == bytes([fill])*16
    assert bytes(u.mem_read(CTX+0x68, 0x28)) == bytes(0x28)
    assert calls == stub_names[1:]
    captured['status'] = struct.unpack('<I', u.mem_read(captured.pop('status_pointer'), 4))[0]
    captured['return'] = u.reg_read(regs.UC_ARM64_REG_X0) & 0xffffffff
    captured['original_rvas'] = [hex(rva) for rva in sorted(reached)]
    return captured


def linux(lib, blob, bus):
    source = ctypes.create_string_buffer(blob)
    output = ctypes.create_string_buffer(len(blob))
    words = (ctypes.c_uint32*3)()
    assert lib.n1x_sroot_stage_image(output, len(blob), source, len(blob), DMA,
                                    0xe0000000, 0xffffffff, words) == 0

    @READ
    def read(_, offset, out):
        out[0] = bus.read(offset)
        return 0

    @WRITE
    def write(_, offset, value):
        bus.write(offset, value)
        return 0

    @DELAY
    def delay(_, interval):
        bus.trace.append(('d', interval))

    io, reply = IO(None, write, read, delay), Reply()
    assert lib.n1x_sroot_exchange(ctypes.byref(io), bus.stride, 0x18, words, 3,
                                  ctypes.byref(reply), 4, 1) == 0
    return output.raw, list(words), reply


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    kmd = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(kmd.read_bytes()).hexdigest() == KMD_SHA256
    kmd_pe = pefile.PE(str(kmd), fast_load=True)
    images = [kmd_pe.get_data(rva, 67508) for rva in (0x1405800, 0x13f5040)]
    hashes = [IMAGE_HASH, '6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4']
    assert all(len(blob) == 67508 and hashlib.sha256(blob).hexdigest() == digest
               for blob, digest in zip(images, hashes))
    with tempfile.TemporaryDirectory(prefix='n1x-full-upload-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    lib.n1x_sroot_stage_image.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32)]
    lib.n1x_sroot_exchange.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint8,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint8, ctypes.POINTER(Reply),
        ctypes.c_uint32, ctypes.c_uint32]
    rows = []
    for variant, blob in enumerate(images):
        for header, fill in product((LIVE_REPLY, FALLBACK_LIVE_REPLY, 0x18), (0, 0xa5)):
            original, candidate = Bus(16, header, []), Bus(16, header, [])
            observed = windows(pe, blob, original, fill)
            staged, words, reply = linux(lib, blob, candidate)
            assert observed['words'] == words == [0xc0302, len(blob), DMA]
            assert observed['staged'] == blob[4:]+bytes([fill])*4
            assert staged == blob[4:]+bytes(4)
            assert observed['status'] == reply.status | reply.detail << 12
            assert reply.header == header and reply.command == 0x18 and reply.count == 0
            assert observed['return'] == (0 if header == 0x18 else 0xc0000229)
            assert original.tx == candidate.tx == [0x318, *words]
            assert original.trace == [item for item in candidate.trace if item[0] != 'd']
            rows.append({'variant': variant, 'image_sha256': hashes[variant],
                'reply': hex(header), 'allocation_fill': hex(fill),
                'image_reply_pair_observed_on_hardware': header == (LIVE_REPLY, FALLBACK_LIVE_REPLY)[variant],
                'windows_return': hex(observed['return']), 'decoded_status': hex(observed['status']),
                'transmitted_words': [hex(word) for word in original.tx],
                'matching_mmio_events': len(original.trace),
                'windows_tail': observed['staged'][-4:].hex(), 'linux_tail': staged[-4:].hex(),
                'original_rvas': observed['original_rvas']})
    print(json.dumps({'passed': True, 'hardware_access': False,
        'driver_sha256': HASH, 'image_bytes': len(blob),
        'dma_address': hex(DMA), 'cases': rows,
        'limitations': ['OS/KMDF, DMA allocator, CFG check and mailbox replies are modeled',
            'Original handler, validation, allocation caller, memcpy, request, exchange and cleanup execute',
            'Synthetic success is a control, not firmware acceptance',
            'No physical cache, IOMMU translation, SROOT image read or error meaning is established',
            'Windows allocator tail is unknown; the observed copy leaves its final four bytes untouched',
            'Windows cleanup is not proof of safe Linux DMA release']}, indent=2))


if __name__ == '__main__':
    main()
