#!/usr/bin/env python3
"""Exercise actual RISC-V firmware task-header checks in isolated RAM.

Only version/engine/queue/next-pointer checks run. This does not execute a task,
firmware initialization, DMA or any hardware register operation.
"""
import hashlib
import json
import struct
from pathlib import Path
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE
from unicorn import riscv_const as regs

ROOT = Path(__file__).resolve().parents[1]
BLOB_HASH = '8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75'
CODE_BASE = 0x100000
HEADER = 0x20000000
OUTCOMES = {0x104b78: 'accepted_common_header', 0x104d32: 'invalid_version',
            0x104b10: 'invalid_engine', 0x104b36: 'invalid_queue', 0x104b5e: 'invalid_next'}


def run(code, version=2, engine=0x44, queue=0, next_pointer=0, raw_header=None):
    uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    uc.mem_map(CODE_BASE, 0xb000)
    uc.mem_write(CODE_BASE, code)
    uc.mem_map(HEADER, 0x1000)
    header = bytearray(60)
    struct.pack_into('<QBBH', header, 0, next_pointer, version, engine, len(header))
    header[20] = queue
    if raw_header is not None:
        assert len(raw_header) == 60
        header = raw_header
    uc.mem_write(HEADER, bytes(header))
    uc.reg_write(regs.UC_RISCV_REG_S3, HEADER)
    outcome = []

    def stop(_, address, size, user):
        if address in OUTCOMES:
            outcome.append(OUTCOMES[address])
            uc.emu_stop()

    uc.hook_add(UC_HOOK_CODE, stop)
    uc.emu_start(0x104d20, CODE_BASE+len(code), count=50)
    assert len(outcome) == 1, 'unexpected path or instruction budget exhausted'
    return outcome[0]


def umd_stride(code, version):
    uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    uc.mem_map(CODE_BASE, 0xb000)
    uc.mem_write(CODE_BASE, code)
    uc.mem_map(0x189000, 0x1000)
    # AUIPC+LBU at 0x1018e4 reads the first copied UMD header byte here.
    uc.mem_write(0x189250, bytes([version]))
    result = []

    def stop(_, address, size, user):
        if address == 0x101932:
            result.append(uc.reg_read(regs.UC_RISCV_REG_S8))
            uc.emu_stop()
        elif address == 0x1018fc:
            result.append(None)
            uc.emu_stop()

    uc.hook_add(UC_HOOK_CODE, stop)
    uc.emu_start(0x1018e4, CODE_BASE+len(code), count=20)
    assert len(result) == 1
    return result[0]


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    code = blob[0x200:0xb100]
    cases = 0
    accepted_versions = []
    for value in range(256):
        outcome = run(code, version=value)
        if outcome == 'accepted_common_header':
            accepted_versions.append(value)
        else:
            assert outcome == 'invalid_version'
        cases += 1
    assert accepted_versions == [2, 3]
    for value in range(256):
        assert run(code, engine=value) == ('accepted_common_header' if value == 0x44 else 'invalid_engine')
        assert run(code, queue=value) == ('accepted_common_header' if value == 0 else 'invalid_queue')
        cases += 2
    for pointer in (0, 0x100, 0x100000000, 0x1234567800):
        assert run(code, next_pointer=pointer) == 'accepted_common_header'
        cases += 1
    for pointer in (1, 4, 0xff, 0x101, 0x100000001):
        assert run(code, next_pointer=pointer) == 'invalid_next'
        cases += 1
    umd_versions = {}
    for version in range(256):
        stride = umd_stride(code, version)
        if stride is not None:
            umd_versions[version] = stride
        cases += 1
    assert umd_versions == {1: 36, 2: 64}
    print(json.dumps({'scope': 'Original RISC-V common task-header check slice in RAM',
                      'firmware_sha256': BLOB_HASH, 'entry': '0x104d20',
                      'verified_cases': cases, 'accepted_versions': accepted_versions,
                      'version_offset': 8, 'required_engine': '0x44', 'engine_offset': 9,
                      'only_accepted_queue': 0, 'queue_offset': 20,
                      'next_pointer_offset': 0, 'next_pointer_alignment': 256,
                      'header_copy_bytes_observed_at_0x104cc4': 60,
                      'separate_umd_version_stride_bytes': umd_versions,
                      'limitations': ['Checks stop before task scheduling',
                                      'Header size, address list, actions and timeout are not validated by this slice',
                                      'No proof of full descriptor or inference compatibility'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
