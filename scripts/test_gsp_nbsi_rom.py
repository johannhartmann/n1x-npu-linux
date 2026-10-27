#!/usr/bin/env python3
"""Replay the NBSI ROM-image selector, not a live NBSI request.

Synthetic PCI ROM images only; original firmware parser executes in Unicorn.
Only logging is stubbed. This tests the selector called after ROM-cache copying,
not the full NBSI object parser or its reachability from a public control call.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn.riscv_const import UC_RISCV_REG_A4
from test_gsp_rom_source import run
from test_gsp_vbios_export import BLOB, GPU, ELF_SHA256, machine


def rom_image(code_type, *, last=True):
    data = bytearray(512)
    data[:2] = b"\x55\xaa"
    struct.pack_into("<H", data, 0x18, 0x20)
    data[0x20:0x24] = b"PCIR"
    struct.pack_into("<H", data, 0x2A, 0x20)  # PCI data-structure length.
    struct.pack_into("<H", data, 0x30, 1)  # Image size in 512-byte units.
    data[0x34], data[0x35] = code_type, 0x80 if last else 0
    # Selector checks numeric little-endian 0x4e425349 (bytes ISBN).
    struct.pack_into("<I", data, 0x40, 0x4E425349)
    data[0x48:0x4A] = b"\x00\x01"
    return bytes(data)


def replay(binary, name, data, expected_offset):
    u = machine(binary)
    u.mem_write(BLOB, data)
    out_pointer, out_size = GPU + 0x9000, GPU + 0x9010
    u.reg_write(UC_RISCV_REG_A4, out_size)
    status = run(u, 0x1C1ADB4, (0, BLOB, len(data), out_pointer))
    pointer = struct.unpack("<Q", u.mem_read(out_pointer, 8))[0]
    size = struct.unpack("<I", u.mem_read(out_size, 4))[0]
    if expected_offset is None:
        assert status == 0xFFFF and pointer == 0 and size == 0
    else:
        assert status == 0 and pointer == BLOB + expected_offset
        assert size == 512 - 0x40
        assert bytes(u.mem_read(pointer, 4)) == b"ISBN"
    return dict(case=name, status=status, selected_offset=pointer - BLOB if pointer else None,
                selected_size=size, passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    args = parser.parse_args()
    binary = args.elf.read_bytes()
    if hashlib.sha256(binary).hexdigest() != ELF_SHA256:
        parser.error("Unsupported firmware SHA256")
    cases = [replay(binary, "ordinary_rom_not_returned", rom_image(0), None),
             replay(binary, "nbsi_payload_only", rom_image(0x70), 0x40),
             replay(binary, "skip_ordinary_image", rom_image(0, last=False) +
                    rom_image(0x70), 0x240),
             replay(binary, "bad_rom_signature", bytes(512), None)]
    print(json.dumps(dict(elf_sha256=ELF_SHA256, entry="0x1c1adb4", cases=cases), indent=2))


if __name__ == "__main__":
    main()
