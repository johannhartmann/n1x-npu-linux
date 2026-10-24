#!/usr/bin/env python3
"""Verify the pinned Windows word-transfer routine against RAM-only callbacks.

This records abstract interface calls, not physical register addresses. No device
is opened, and the shared readback verification is deliberately stubbed.
"""
import hashlib
import json
import struct
from pathlib import Path

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 0x140000000
ENTRY = IMAGE + 0xd4bbe0
OBJ, VTABLE, FW, HEADER, STACK, STUBS = (
    0x20000000, 0x20100000, 0x20200000, 0x20300000, 0x20400000, 0x20500000)
STOP = STUBS + 0xff0
RET = bytes.fromhex('c0035fd6')


def run(pe, blob, fail_slot=None):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((IMAGE+0xd4b000, 0x2000), (IMAGE+0xd57000, 0x1000),
                       (IMAGE+0xce000, 0x1000), (IMAGE+0xe3e000, 0x1000),
                       (OBJ, 0x1000), (VTABLE, 0x1000), (FW, 0x20000),
                       (HEADER, 0x1000), (STACK, 0x10000), (STUBS, 0x1000)):
        uc.mem_map(addr, size)
    uc.mem_write(ENTRY, pe.get_data(0xd4bbe0, 0x39c))
    for address in (IMAGE+0xd57b60, IMAGE+0xd57b80, IMAGE+0xce4b0,
                    IMAGE+0xd4cad8, STUBS+0x800):
        uc.mem_write(address, RET)
    uc.mem_write(IMAGE+0xe3eaf0, struct.pack('<Q', STUBS+0x800))
    slots = (0x118, 0x40, 0x60, 0x50, 0x70, 0x90, 0x80, 0xf8)
    for slot in slots:
        uc.mem_write(VTABLE+slot, struct.pack('<Q', STUBS+slot))
        uc.mem_write(STUBS+slot, RET)
    uc.mem_write(OBJ, struct.pack('<Q', VTABLE))
    uc.mem_write(FW, blob[0x200:])
    uc.mem_write(HEADER, struct.pack('<6I', 0, 0, 0, 0xaf00, 0xaf00, 0xa400))
    calls = []
    continuation = []

    def reg(n):
        return uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))

    def hook(_, address, size, user):
        slot = address-STUBS
        if slot in slots:
            # Slot 0x118 is a read/poll callback. The rest consume one u32.
            ptr = reg(2) if slot in (0x118, 0xf8) else reg(3)
            value = struct.unpack('<I', uc.mem_read(ptr, 4))[0]
            calls.append((slot, value))
            uc.reg_write(regs.UC_ARM64_REG_X0, 0x65 if slot == fail_slot else 0)
        elif address == IMAGE+0xd4cad8:
            continuation.append([reg(i) for i in range(1, 5)])
            uc.reg_write(regs.UC_ARM64_REG_X0, 0)

    uc.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0, OBJ, FW, HEADER)):
        uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
    uc.emu_start(ENTRY, STOP, count=2000000)
    assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP, 'instruction budget exhausted'
    return uc.reg_read(regs.UC_ARM64_REG_X0), calls, continuation


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == '8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75'
    status, calls, continuation = run(pe, blob)
    expected = [(0x118, 0), (0x40, 0x1000000)]
    for data, word_slot, block_slot in (
            (blob[0x200:0xb100], 0x50, 0x60),
            (blob[0xb100:0x15500], 0x80, 0x90)):
        if word_slot == 0x80:
            expected.append((0x70, 0x1000000))
        for index, (word,) in enumerate(struct.iter_unpack('<I', data)):
            if index % 64 == 0:
                expected.append((block_slot, index // 64))
            expected.append((word_slot, word))
    expected.append((0xf8, 3))
    assert status == 0 and calls == expected
    assert continuation == [[OBJ, FW, HEADER, 0]]
    failures = []
    for slot in (0x118, 0x40, 0x60, 0x50, 0x70, 0x90, 0x80, 0xf8):
        status, trace, tail = run(pe, blob, slot)
        assert status == 0x65 and trace[-1][0] == slot and not tail
        failures.append(hex(slot))
    print(json.dumps({
        'scope': 'Offline ARM64 transfer; abstract callbacks and readback verification stubbed',
        'entry_rva': hex(ENTRY-IMAGE), 'status': 0, 'verified_callback_count': len(calls),
        'code_words': 0xaf00//4, 'data_words': 0xa400//4,
        'block_bytes': 256, 'code_block_count': 0xaf00//256,
        'data_block_count': 0xa400//256, 'readback_verification_rva': '0xd4cad8',
        'failure_propagation_slots': failures,
        'limitations': ['Physical registers and hardware selector are not established',
                        'Read callback returns synthetic success; readback verification is stubbed']}, indent=2))


if __name__ == '__main__':
    main()
