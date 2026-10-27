#!/usr/bin/env python3
"""Replay original component-9 ROM eligibility branches, offline only.

The protobuf encoder and register-dump callback are explicit stubs. They expose
controlled remaining space; no wire encoding or live ROM availability is tested.
The hash-pinned firmware executes the pointer, size, flag and error branches.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.riscv_const import (
    UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_A2,
    UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP,
)
from test_gsp_vbios_export import ELF_SHA256, GPU, BLOB, STOP, machine

ENTRY = 0x11BD55C
OBJ, ENCODER, CALLBACK = GPU + 0x3000, GPU + 0x6000, STOP + 0x100
BEGIN, FIELD, END, LOG = 0x1057F44, 0x10580B4, 0x10586C0, 0x1AD7214
RAW_ROM, TABLES = 0x1C4CAC8, 0x1C4CA50


def replay(binary, space, size, *, flag=0, present=True, callback_status=0,
           callback_cost=0):
    u = machine(binary)
    assert struct.unpack("<I", u.mem_read(0x1C4CAE0, 4))[0] == 11
    assert struct.unpack("<I", u.mem_read(RAW_ROM, 4))[0] == 10

    def put(address, value, fmt="<Q"):
        u.mem_write(address, struct.pack(fmt, value))

    def get(address, fmt="<Q"):
        return struct.unpack(fmt, u.mem_read(address, struct.calcsize(fmt)))[0]

    put(GPU + 0x2060, OBJ)
    put(OBJ + 0x208, CALLBACK)
    put(OBJ + 0x2A8, BLOB if present else 0)
    put(OBJ + 0x2B0, size, "<I")
    put(OBJ + 0xFA8, flag, "<I")
    u.mem_write(CALLBACK, b"\x67\x80\x00\x00")  # Valid ret for Unicorn decoding.
    # Distinct encoder and VBIOS objects: VBIOS extends past offset 0x1000.
    for reg, value in ((UC_RISCV_REG_A0, GPU), (UC_RISCV_REG_A1, ENCODER),
                       (UC_RISCV_REG_A2, 0), (UC_RISCV_REG_SP, STOP + 0xF000),
                       (UC_RISCV_REG_RA, STOP)):
        u.reg_write(reg, value)
    messages, raw_fields, callbacks, logs, flag_fields = [], [], [], [], []

    def finish(value=0):
        u.reg_write(UC_RISCV_REG_A0, value)
        u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    def hook(uc, pc, instruction_size, user_data):
        if pc in (BEGIN, FIELD, END):
            assert u.reg_read(UC_RISCV_REG_A0) == ENCODER
        if pc == BEGIN:
            messages.append(u.reg_read(UC_RISCV_REG_A1))
            depth = get(ENCODER + 0x148, "<I")
            assert depth < 3
            put(ENCODER + depth * 0x28 + 8, BLOB)
            put(ENCODER + depth * 0x28 + 16, BLOB + space)
            put(ENCODER + 0x148, depth + 1, "<I")
            finish()
        elif pc == END:
            depth = get(ENCODER + 0x148, "<I")
            assert depth > 0
            put(ENCODER + 0x148, depth - 1, "<I")
            finish()
        elif pc == FIELD:
            if u.reg_read(UC_RISCV_REG_A1) == 0x1C4CAE0:
                flag_fields.append(get(u.reg_read(UC_RISCV_REG_A2), "<I"))
            if u.reg_read(UC_RISCV_REG_A1) == RAW_ROM:
                value = u.reg_read(UC_RISCV_REG_A2)
                raw_fields.append((get(value), get(value + 8, "<I")))
            finish()
        elif pc == CALLBACK:
            callbacks.append(pc)
            assert 0 <= callback_cost <= space
            depth = get(ENCODER + 0x148, "<I")
            put(ENCODER + (depth - 1) * 0x28 + 8, BLOB + callback_cost)
            finish(callback_status)
        elif pc == LOG:
            logs.append(pc)
            finish()
        elif pc in (0x1BA0724, 0x1ADD13E):
            raise AssertionError(f"Firmware assertion/canary failure at {pc:#x}")

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(ENTRY, STOP, count=100_000)
    assert u.reg_read(UC_RISCV_REG_PC) == STOP, "Instruction bound exceeded"
    assert get(ENCODER + 0x148, "<I") == 0, "Unbalanced encoder frames"
    assert len(callbacks) == 1
    assert flag_fields == [flag]
    budget = (space - callback_cost) >> (1 if flag else 2)
    expected = bool(present and size < budget and callback_status == 0)
    assert raw_fields == ([(BLOB, size)] if expected else [])
    assert (TABLES in messages) == (space > 65536)
    assert u.reg_read(UC_RISCV_REG_A0) == callback_status
    assert bool(logs) == bool(callback_status)
    return dict(space=space, size=size, flag=flag, present=present,
                callback_status=callback_status, callback_cost=callback_cost,
                raw_rom_selected=expected, table_branch=TABLES in messages,
                passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    args = parser.parse_args()
    binary = args.elf.read_bytes()
    if hashlib.sha256(binary).hexdigest() != ELF_SHA256:
        parser.error("Unsupported firmware SHA256")
    cases = []
    for space in (65535, 65536, 65537, 131072, 262148):
        for flag in (0, 1, 2):
            budget = space >> (1 if flag else 2)
            for size in (0, budget - 1, budget, budget + 1, 32768, 65536):
                cases.append(replay(binary, space, size, flag=flag))
    cases.append(replay(binary, 65536, 1, present=False))
    cases.append(replay(binary, 65536, 1, callback_status=0x51))
    cases.append(replay(binary, 65536, 16383, callback_cost=4))
    print(json.dumps({"elf_sha256": ELF_SHA256, "entry": hex(ENTRY),
                      "encoder_stubbed": True, "register_reads_stubbed": True,
                      "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
