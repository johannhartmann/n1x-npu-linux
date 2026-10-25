#!/usr/bin/env python3
"""Replay the pinned GSP init FRTS handoff offline; no hardware access.

Runs original RV64 basic blocks. Mapping, logging and the LibOS receive syscall
are substituted with synthetic data. Does not execute secure boot, establish
live addresses, or prove that the captured board ROM contains an NPU policy.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE
from unicorn.riscv_const import (
    UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_A2, UC_RISCV_REG_A3,
    UC_RISCV_REG_A4, UC_RISCV_REG_A5, UC_RISCV_REG_T0,
    UC_RISCV_REG_S3, UC_RISCV_REG_S4, UC_RISCV_REG_S5, UC_RISCV_REG_S6, UC_RISCV_REG_S9,
    UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP,
)
from test_gsp_vbios_export import machine as rm_machine, ELF_SHA256
from test_gsp_rom_source import run

INIT_SHA256 = "46b99be784a02ec8221b25d6cadaa6bf03eeadd45f5234de286eefa78213f028"
BLOCK, WPR_SLOT, META = 0x40020D8, 0x40025B0, 0x6000000
STOP, STACK, CONTAINER = 0x7000000, 0x700F000, 0x8000000
ARGS = (UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_A2,
        UC_RISCV_REG_A3, UC_RISCV_REG_A4)


def put64(u, address, value):
    u.mem_write(address, struct.pack("<Q", value))


def get64(u, address):
    return struct.unpack("<Q", u.mem_read(address, 8))[0]


def init_machine(binary):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for va, off, size in ((0x1000000, 0, 0x8000), (0x4000000, 0x8000, 0x3000)):
        u.mem_map(va, size)
        u.mem_write(va, binary[off:off + size])
    for va in (META, STOP, CONTAINER):
        u.mem_map(va, 0x10000)
    u.reg_write(UC_RISCV_REG_SP, STACK)
    u.reg_write(UC_RISCV_REG_RA, STOP)
    return u


def replay(init, rm, *, base, offset, length, map_status=0):
    u = init_machine(init)
    put64(u, WPR_SLOT, META)
    put64(u, META + 0x98, offset)
    put64(u, META + 0xA0, length)
    for reg, value in ((UC_RISCV_REG_S3, 17), (UC_RISCV_REG_S4, base),
                       (UC_RISCV_REG_S5, WPR_SLOT), (UC_RISCV_REG_S9, BLOCK)):
        u.reg_write(reg, value)
    maps = []

    def hook(u, pc, size, _):
        if pc == 0x10024EC:
            args = [u.reg_read(reg) for reg in ARGS]
            assert args == [17, BLOCK + 0x118, (base + offset) & ((1 << 64) - 1), length, 4]
            maps.append(args)
            if not map_status:
                put64(u, args[1], CONTAINER)
            u.reg_write(UC_RISCV_REG_A0, map_status)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))
        elif pc == 0x1001982:  # Logging only.
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))
        elif pc in (0x100530E, 0x100558A):
            u.emu_stop()

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x10052BE, STOP, count=200)
    assert u.reg_read(UC_RISCV_REG_PC) == (0x100558A if length and map_status else 0x100530E)
    assert len(maps) == bool(length)
    assert get64(u, BLOCK + 0x120) == offset
    assert get64(u, BLOCK + 0x128) == length
    assert get64(u, BLOCK + 0x118) == (CONTAINER if length and not map_status else 0)
    record = {"base": hex(base), "offset": hex(offset), "size": length,
              "mapping_status": map_status, "mapping_calls": maps}
    if not length or map_status:
        return record

    # Replay the RM entry prefix and original LibOS receive wrapper. Only the
    # actual syscall transfers our synthetic 656-byte message, as a kernel would.
    block = bytes(u.mem_read(BLOCK, 0x290))
    v = rm_machine(rm)
    v.reg_write(UC_RISCV_REG_SP, STACK)
    v.reg_write(UC_RISCV_REG_A4, 17)
    received = []

    def receive(v, pc, size, _):
        if pc == 0x1ADD146:
            v.reg_write(UC_RISCV_REG_PC, v.reg_read(UC_RISCV_REG_RA))
        elif pc == 0x1BA0012:
            assert v.reg_read(UC_RISCV_REG_T0) == 1
            assert [v.reg_read(reg) for reg in ARGS] == [3, 17, 0x4194810, 0x290, 1]
            assert v.reg_read(UC_RISCV_REG_A5) == (1 << 64) - 1
            v.mem_write(0x4194810, block)
            v.reg_write(UC_RISCV_REG_A0, 0)
            received.append(True)
            v.reg_write(UC_RISCV_REG_PC, pc + 2)

    handle = v.hook_add(UC_HOOK_CODE, receive)
    v.emu_start(0x1AFAB76, 0x1BA0012, count=200)
    assert v.reg_read(UC_RISCV_REG_PC) == 0x1BA0012
    # LibOS uses an illegal-instruction trap (0x0000), not a native ecall.
    # Stop before decoding it; supply the syscall result and resume after it.
    assert bytes(v.mem_read(0x1BA0012, 2)) == b"\x00\x00"
    receive(v, 0x1BA0012, 2, None)
    v.emu_start(0x1BA0014, 0x1AFABD2, count=200)
    v.hook_del(handle)
    assert v.reg_read(UC_RISCV_REG_PC) == 0x1AFABD2 and received == [True]
    assert get64(v, 0x4194928) == CONTAINER
    assert get64(v, 0x4194930) == offset
    assert get64(v, 0x4194938) == length

    # Replay the original RM block that installs the received container in the
    # GPU object. The earlier descriptor branch is absent in this synthetic GPU.
    gpu, obj = 0x6000000, 0x6004000
    v.reg_write(UC_RISCV_REG_S6, 0x4194810)
    v.reg_write(UC_RISCV_REG_S4, obj)
    v.emu_start(0x1AC9518, 0x1AC9532, count=30)
    assert v.reg_read(UC_RISCV_REG_PC) == 0x1AC9532
    assert get64(v, obj + 0x3500) == CONTAINER
    assert struct.unpack("<I", v.mem_read(obj + 0x3508, 4))[0] == length
    assert bytes(v.mem_read(obj + 0x378, 1)) == b"\x01"
    put64(v, gpu + 0x22F0, obj)

    # The original HAL reader extracts the ROM member from that object.
    payload = b"SYNTHETIC-ONLY: FRTS -> GFWI -> ROM"
    container = bytearray(128)
    container[:4] = b"GFWI"
    struct.pack_into("<II", container, 32, 64, len(payload))
    container[64:64 + len(payload)] = payload
    v.mem_write(CONTAINER, bytes(container))
    dest, count = 0x8100000, 0x6009000
    v.mem_write(count, struct.pack("<I", 128))
    assert run(v, 0x11BCDC4, (gpu, 0, dest, count)) == 0
    assert bytes(v.mem_read(dest, len(payload))) == payload
    assert struct.unpack("<I", v.mem_read(count, 4))[0] == len(payload)
    record["handoff_and_rom_copy"] = True
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--init-elf", type=Path, required=True)
    p.add_argument("--rm-elf", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("results/gsp-frts-handoff.json"))
    a = p.parse_args()
    init, rm = a.init_elf.read_bytes(), a.rm_elf.read_bytes()
    for name, data, digest in (("init", init, INIT_SHA256), ("rm", rm, ELF_SHA256)):
        if hashlib.sha256(data).hexdigest() != digest:
            p.error(f"Pinned {name} ELF hash mismatch")
    for flag, expected in ((0, 0x100000000), (1, 0x200000000), (255, 0x200000000)):
        u = init_machine(init)
        put64(u, 0x40023B0, 0x100000000)
        put64(u, 0x40023C0, 0x200000000)
        u.mem_write(0x4002548, bytes([flag]))
        u.emu_start(0x1001106, STOP, count=50)
        assert u.reg_read(UC_RISCV_REG_PC) == STOP
        assert u.reg_read(UC_RISCV_REG_A0) == expected
    cases = [replay(init, rm, base=base, offset=off, length=length, map_status=status)
             for base, off, length, status in (
                 (0, 0x12345000, 128, 0), (0x100000000, 0x23456000, 128, 0),
                 (0x200000000, 0x34567080, 128, 0), (0, 0x12345000, 0, 0),
                 (0, 0x12345000, 128, 5))]
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps({"init_sha256": INIT_SHA256, "rm_sha256": ELF_SHA256,
        "scope": __doc__, "base_selector_cases": 3, "cases": cases}, indent=2) + "\n")
    print("PASS: 3 base selections, 5 FRTS cases, 3 original receive/ROM-copy paths")


if __name__ == "__main__":
    main()
