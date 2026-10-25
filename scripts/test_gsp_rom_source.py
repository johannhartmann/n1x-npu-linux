#!/usr/bin/env python3
"""Offline replay of the pinned GSP ROM HAL selection and GFWI byte reader."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.riscv_const import (
    UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_A2, UC_RISCV_REG_A3,
    UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP,
)

from extract_gfwi_vbios import extract_rom, MAX_ROM_SIZE
from test_gsp_vbios_export import machine, ELF_SHA256, LOG, STOP


def run(u, entry, args, extra_hook=None):
    for register, value in zip((UC_RISCV_REG_A0, UC_RISCV_REG_A1,
                               UC_RISCV_REG_A2, UC_RISCV_REG_A3), args):
        u.reg_write(register, value)
    u.reg_write(UC_RISCV_REG_SP, STOP + 0xF000)
    u.reg_write(UC_RISCV_REG_RA, STOP)

    def hook(uc, pc, size, user_data):
        if pc == LOG:
            u.reg_write(UC_RISCV_REG_A0, 0)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))
        elif extra_hook:
            extra_hook(u, pc)

    handle = u.hook_add(UC_HOOK_CODE, hook)
    try:
        u.emu_start(entry, STOP, count=100_000)
        assert u.reg_read(UC_RISCV_REG_PC) == STOP
        return u.reg_read(UC_RISCV_REG_A0)
    finally:
        u.hook_del(handle)


def hal_case(binary, chip, variant):
    u = machine(binary)
    obj, out, gpu_hal, rm_hal = 0x6000000, 0x6009000, 0x600A000, 0x600B000
    u.mem_write(out, struct.pack("<Q", obj))
    u.mem_write(gpu_hal + 16, struct.pack("<H", chip))
    u.mem_write(rm_hal + 16, struct.pack("<H", variant))

    def dynamic_cast(u, pc):
        if pc == 0x18E8824:
            cls = u.reg_read(UC_RISCV_REG_A1)
            assert cls in (0x74BC71, 0x34A6D6)
            u.reg_write(UC_RISCV_REG_A0, gpu_hal if cls == 0x74BC71 else rm_hal)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    # In-place construction and no parent-list insertion; only dynamic casts stubbed.
    assert run(u, 0x1A0E5CC, (out, 0x6008000, 3), dynamic_cast) == 0
    slots = {offset: struct.unpack("<Q", u.mem_read(obj + offset, 8))[0]
             for offset in (0x118, 0x150, 0x240)}
    assert slots == {0x118: 0x11C0B0C, 0x150: 0x11B8424, 0x240: 0x11BCDC4}
    assert run(u, slots[0x150], (0, obj)) == MAX_ROM_SIZE
    return {"chip_index": chip, "rm_variant": variant,
            "slots": {hex(k): hex(v) for k, v in slots.items()}, "passed": True}


def reader_case(binary, name, *, offset=64, length=32, available=128,
                capacity=128, magic=b"GFWI", null_arg=None):
    u = machine(binary)
    src, dest, count = 0x8000000, 0x8100000, 0x6009000
    data = bytearray((n * 7 + 3) & 255 for n in range(512))
    data[:4] = magic
    struct.pack_into("<II", data, 32, offset, length)
    u.mem_write(src, bytes(data))
    u.mem_write(dest, b"\xA5" * 256)
    u.mem_write(count, struct.pack("<I", capacity))
    args = [src, available, dest, count]
    if null_arg is not None:
        args[null_arg] = 0
    rc = run(u, 0x11BCD14, args)
    expected = 31 if null_arg is not None else (
        37 if magic != b"GFWI" or max(offset, length) > available or length > capacity else 0)
    assert rc == expected, (name, rc, expected)
    if rc == 0:
        assert bytes(u.mem_read(dest, length)) == data[offset:offset + length]
        assert struct.unpack("<I", u.mem_read(count, 4))[0] == length
    else:
        assert bytes(u.mem_read(dest, 256)) == b"\xA5" * 256
        assert struct.unpack("<I", u.mem_read(count, 4))[0] == capacity
    assert bytes(u.mem_read(dest + length, 256 - length)) == b"\xA5" * (256 - length)
    try:
        extracted = extract_rom(bytes(data[:available]))
    except ValueError:
        extracted = None
    # The native file decoder has no output-capacity or null-pointer parameter.
    should_extract = (magic == b"GFWI" and offset >= 40 and
                      0 < length <= MAX_ROM_SIZE and offset + length <= available)
    assert (extracted is not None) == should_extract
    if extracted is not None:
        assert extracted == data[offset:offset + length]
    return {"case": name, "gsp_status": rc, "file_decoder_accepts": should_extract,
            "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/gsp-rom-source.json"))
    args = parser.parse_args()
    binary = args.elf.read_bytes()
    if hashlib.sha256(binary).hexdigest() != ELF_SHA256:
        parser.error("Pinned GSP ELF hash mismatch")
    hals = [hal_case(binary, chip, variant)
            for chip in (61, 62, 70, 71) for variant in range(4)]
    readers = [reader_case(binary, "valid"), reader_case(binary, "unaligned", offset=41),
               reader_case(binary, "bad_magic", magic=b"NOPE"),
               reader_case(binary, "offset_beyond", offset=129),
               reader_case(binary, "length_beyond", length=129),
               reader_case(binary, "capacity_short", capacity=31),
               reader_case(binary, "combined_overrun", offset=120),
               reader_case(binary, "zero_length", length=0),
               reader_case(binary, "header_overlap", offset=8)]
    readers += [reader_case(binary, f"null_{index}", null_arg=index) for index in (0, 2, 3)]
    for n in range(40):
        try:
            extract_rom((b"GFWI" + bytes(36))[:n])
        except ValueError:
            pass
        else:
            raise AssertionError("Truncated header accepted")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"elf_sha256": ELF_SHA256,
        "scope": "Original constructor and GFWI copy helper; dynamic casts and logs "
                 "stubbed. No physical ROM read or platform-source acquisition.",
        "hal_cases": hals, "reader_cases": readers}, indent=2) + "\n")
    print(f"PASS: {len(hals)} HAL selections, {len(readers)} GFWI cases and 40 short headers")


if __name__ == "__main__":
    main()
