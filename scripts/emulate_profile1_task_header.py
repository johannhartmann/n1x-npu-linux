#!/usr/bin/env python3
"""Revalidate the actual HAL-1 RV32 task header, queue check and initial DMA copy."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT, STACK, STOP

HEADER, CTX = 0x20000000, 0x20010000
OUTCOMES = {0x1045bc: 'accepted_common_header', 0x10470c: 'invalid_version',
            0x10454a: 'invalid_engine', 0x104574: 'invalid_queue', 0x1045a2: 'invalid_next'}


def make_header(version=2, engine=0x44, queue=0, next_pointer=0):
    header = bytearray(60)
    struct.pack_into('<QBBH', header, 0, next_pointer, version, engine, len(header))
    header[20] = queue
    return header


def check(code, raw_header=None, **fields):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    u.mem_map(0x100000, 0x9000)
    u.mem_write(0x100000, code)
    u.mem_map(HEADER-0x1000, 0x2000)
    u.mem_write(HEADER, bytes(make_header(**fields) if raw_header is None else raw_header))
    u.reg_write(regs.UC_RISCV_REG_S1, HEADER-256)
    u.reg_write(regs.UC_RISCV_REG_S3, HEADER-256)
    u.reg_write(regs.UC_RISCV_REG_S2, 0)
    result = []

    def hook(_, address, size, user):
        if address in OUTCOMES:
            result.append(OUTCOMES[address])
            u.emu_stop()

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x1046fa, 0x109000, count=100)
    assert len(result) == 1
    return result[0]


def initial_copy(blob, address=0x20001000):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for base, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                       (CTX, 0x4000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(base, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    u.mem_write(0x1862bc, struct.pack('<I', CTX))
    u.mem_write(0x104ed8, bytes.fromhex('67800000'))
    raw = bytes(make_header())
    transfers = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}'))

    def hook(_, pc, size, user):
        if pc == 0x104ed8:
            source = reg(0) | reg(1) << 32
            assert source == address and reg(2) == CTX+256 and reg(3) == 60 and reg(4) == 0
            transfers.append({'source': hex(source), 'destination': hex(reg(2)), 'length': reg(3)})
            u.mem_write(reg(2), raw)
            u.reg_write(regs.UC_RISCV_REG_A0, 0)

    u.hook_add(UC_HOOK_CODE, hook)
    assert address & 255 == 0 and address >> 8 <= 0xffffffff
    u.reg_write(regs.UC_RISCV_REG_A0, address >> 8)
    u.reg_write(regs.UC_RISCV_REG_A1, 1)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x104458, 0x1046fa, count=10000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == 0x1046fa
    assert bytes(u.mem_read(CTX+256, 60)) == raw and len(transfers) == 1
    return transfers[0]


def umd_stride(code, version):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    u.mem_map(0x100000, 0x9000)
    u.mem_write(0x100000, code)
    u.mem_map(0x185000, 0x1000)
    u.mem_write(0x185d04, bytes([version]))
    u.mem_map(STACK, 0x1000)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK)
    result = []

    def hook(_, address, size, user):
        if address == 0x10179e:
            result.append(struct.unpack('<I', u.mem_read(STACK+28, 4))[0])
            u.emu_stop()
        elif address == 0x101780:
            result.append(None)
            u.emu_stop()

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x101766, 0x109000, count=30)
    assert len(result) == 1
    return result[0]


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    code = blob[512:512+36864]
    cases, versions = 0, []
    for value in range(256):
        outcome = check(code, version=value)
        if outcome == 'accepted_common_header':
            versions.append(value)
        else:
            assert outcome == 'invalid_version'
        cases += 1
        assert check(code, engine=value) == ('accepted_common_header' if value == 0x44 else 'invalid_engine')
        assert check(code, queue=value) == ('accepted_common_header' if value == 0 else 'invalid_queue')
        cases += 2
    assert versions == [2, 3]
    for pointer in (0, 0x100, 0x100000000, 0x1234567800, 0xffffffffffffff00):
        assert check(code, next_pointer=pointer) == 'accepted_common_header'
        cases += 1
    for pointer in (1, 4, 0xff, 0x101, 0x100000001):
        assert check(code, next_pointer=pointer) == 'invalid_next'
        cases += 1
    strides = {version: stride for version in range(256)
               if (stride := umd_stride(code, version)) is not None}
    assert strides == {1: 36, 2: 64}
    copies = [initial_copy(blob, address) for address in (0x20001000, 0x1234567800, 0xfffffffe00)]
    print(json.dumps({'passed': True, 'hardware_access': False, 'architecture': 'RV32',
                      'firmware_sha256': PROFILE1_HASH, 'verified_header_cases': cases,
                      'accepted_versions': versions, 'required_engine': '0x44', 'queue': 0,
                      'next_alignment': 256, 'descriptor_dma_copies': copies,
                      'umd_version_cases': 256, 'umd_version_strides': strides,
                      'limitations': ['Synthetic context/pool and stubbed DMA copy',
                                      'Stops before enqueue, scheduling or descriptor extensions',
                                      'Common-header acceptance is not complete task execution or inference']}, indent=2))


if __name__ == '__main__':
    main()
