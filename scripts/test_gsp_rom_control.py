#!/usr/bin/env python3
"""Execute GSP ROM control 0x20800801 and its original cached-byte reader.

Offline synthetic cache only, no device access. Only logging is stubbed.
This establishes firmware-side semantics, not Linux pointer marshalling.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.riscv_const import UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP
from test_gsp_vbios_export import machine, ELF_SHA256, GPU, BLOB, HEAP, STOP, LOG


def replay(binary, requested, *, present=True):
    u = machine(binary)
    obj, subdevice, device, params = GPU+0x3000, GPU+0x8000, GPU+0x9000, GPU+0xA000
    data = b"\x55\xaaSYNTHETIC-ROM-CACHE" + bytes(range(32))
    def put64(address, value):
        u.mem_write(address, struct.pack("<Q", value))
    put64(GPU+0x2060, obj if present else 0)
    put64(subdevice+0x160, device)
    put64(device+0xE8, GPU)
    put64(0x41AE3D8, GPU)
    put64(obj+0x2A8, BLOB)
    u.mem_write(obj+0x2B0, struct.pack("<I", len(data)))
    u.mem_write(BLOB, data)
    u.mem_write(HEAP, b"\xa5"*256)
    u.mem_write(params, struct.pack("<IIQ", requested, 0, HEAP if requested else 0))
    # Export descriptor: handler, flags, command, parameter size.
    assert struct.unpack("<QQII", u.mem_read(0x1C5A898, 24)) == (
        0x1420110, 0x40048, 0x20800801, 16)
    for reg, value in ((UC_RISCV_REG_A0, subdevice), (UC_RISCV_REG_A1, params),
                       (UC_RISCV_REG_SP, STOP+0xF000), (UC_RISCV_REG_RA, STOP)):
        u.reg_write(reg, value)
    def hook(uc, pc, size, user):
        if pc == LOG:
            u.reg_write(UC_RISCV_REG_A0, 0)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))
        elif pc in (0x1BA0724, 0x1ADD13E):
            raise AssertionError(f"Firmware assertion at {pc:#x}")
    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x1420110, STOP, count=1_000_000)
    assert u.reg_read(UC_RISCV_REG_PC) == STOP
    status = u.reg_read(UC_RISCV_REG_A0)
    returned = struct.unpack("<I", u.mem_read(params, 4))[0]
    expected_status = 0x1F if present and requested == 0 else 0
    count = min(requested, len(data)) if present else 0
    assert status == expected_status and returned == count
    assert bytes(u.mem_read(HEAP, 256)) == data[:count]+b"\xa5"*(256-count)
    return dict(requested=requested, cache_present=present, returned=returned,
                status=status, passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    args = parser.parse_args()
    binary = args.elf.read_bytes()
    if hashlib.sha256(binary).hexdigest() != ELF_SHA256:
        parser.error("Unsupported firmware SHA256")
    cases = [replay(binary, size, present=present)
             for present in (True, False) for size in (0, 1, 16, 54, 128)]
    print(json.dumps(dict(command="0x20800801", elf_sha256=ELF_SHA256,
                         linux_marshalling_verified=False, cases=cases), indent=2))


if __name__ == "__main__":
    main()
