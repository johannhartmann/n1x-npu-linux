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
    UC_RISCV_REG_A5, UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP,
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


def fsp_hal_case(binary, chip, variant):
    u = machine(binary)
    obj, out, gpu_hal, rm_hal = 0x6000000, 0x6009000, 0x600A000, 0x600B000
    # The class record gives size 0x300, ID 0x87fb96 and this constructor.
    assert struct.unpack("<IIQQ", u.mem_read(0x1CA8400, 24)) == (
        0x300, 0x87FB96, 0x1EC7448, 0x19B3F88)
    u.mem_write(out, struct.pack("<Q", obj))
    u.mem_write(gpu_hal + 16, struct.pack("<H", chip))
    u.mem_write(rm_hal + 16, struct.pack("<H", variant))

    def dynamic_cast(u, pc):
        if pc == 0x18E8824:
            cls = u.reg_read(UC_RISCV_REG_A1)
            assert cls in (0x74BC71, 0x34A6D6)
            u.reg_write(UC_RISCV_REG_A0, gpu_hal if cls == 0x74BC71 else rm_hal)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    assert run(u, 0x19B3F88, (out, 0x6008000, 3), dynamic_cast) == 0
    missing = bytes(u.mem_read(obj + 0x58, 1))[0]
    assert missing == int(chip in (61, 62, 70, 71))
    assert bytes(u.mem_read(obj + 0x1E9, 1)) == b"\x00"
    assert struct.unpack("<Q", u.mem_read(obj + 0x1F0, 8))[0] == 0
    prepare = struct.unpack("<Q", u.mem_read(obj + 0x150, 8))[0]
    assert prepare == (0x19AA23C if missing else 0x150EE60)
    if missing:
        assert run(u, prepare, (0, obj)) == 0
    return {"chip_index": chip, "rm_variant": variant, "missing_property": missing,
            "source_enabled": False, "descriptor": 0, "prepare_slot": hex(prepare),
            "passed": True}


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


def source_case(binary, name, *, present=True, enabled=True, descriptor=True,
                size=128, fallback=True, magic=b"GFWI", capacity=128, descriptor_field=1):
    """Replay the complete HAL with two distinguishable synthetic sources."""
    u = machine(binary)
    gpu, fsp, desc, other = 0x6000000, 0x6003000, 0x6003800, 0x6004000
    src, alternate, dest, heap, count = 0x8000000, 0x8010000, 0x8100000, 0x8200000, 0x6009000

    def put64(address, value):
        u.mem_write(address, struct.pack("<Q", value))

    def container(payload, signature):
        data = bytearray(128)
        data[:4] = signature
        struct.pack_into("<II", data, 32, 64, len(payload))
        data[64:64 + len(payload)] = payload
        return bytes(data)

    primary_payload, fallback_payload = b"SYNTHETIC-KERNELFSP", b"SYNTHETIC-INIT-FRTS"
    put64(gpu + 0x22D0, fsp if present else 0)
    put64(gpu + 0x22F0, other if fallback else 0)
    u.mem_write(fsp + 0x1E9, bytes([enabled]))
    put64(fsp + 0x1F0, desc if descriptor else 0)
    put64(desc + 0x30, size)
    u.mem_write(desc + 0x8C, struct.pack("<I", descriptor_field))
    put64(desc + 0x110, src)
    u.mem_write(other + 0x378, b"\x01")
    put64(other + 0x3500, alternate)
    u.mem_write(other + 0x3508, struct.pack("<I", 128))
    u.mem_write(src, container(primary_payload, magic))
    u.mem_write(alternate, container(fallback_payload, b"GFWI"))
    u.mem_write(dest, b"\xA5" * 128)
    u.mem_write(count, struct.pack("<I", capacity))
    allocations, frees = [], []

    def allocator(u, pc):
        if pc == 0x18E2D78:
            assert u.reg_read(UC_RISCV_REG_A1) == size == 128
            allocations.append(size)
            u.reg_write(UC_RISCV_REG_A0, heap)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))
        elif pc == 0x18E17E4:
            assert u.reg_read(UC_RISCV_REG_A1) == heap
            frees.append(heap)
            u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    # A class-lookup fragment independently identifies GPU+0x22d0. It is not
    # a host-kernel pointer or an assertion about the live object's existence.
    u.reg_write(UC_RISCV_REG_A0, gpu)
    u.reg_write(UC_RISCV_REG_A5, 0x87FB96)  # public KernelFsp NVOC class ID
    u.emu_start(0x140D55C, 0x140D570, count=12)
    assert u.reg_read(UC_RISCV_REG_PC) == 0x140D570
    assert u.reg_read(UC_RISCV_REG_A0) == (fsp if present else 0)

    rc = run(u, 0x11BCDC4, (gpu, 0, dest, count), allocator)
    primary_selected = present and enabled
    expected_payload = primary_payload if primary_selected else fallback_payload
    expected = (31 if not descriptor or not size else
                37 if magic != b"GFWI" or capacity < len(primary_payload) else 0
                ) if primary_selected else (0 if fallback else 64)
    assert rc == expected, (name, rc, expected)
    allocated = primary_selected and descriptor and bool(size)
    assert len(allocations) == len(frees) == int(allocated)
    if allocated:
        assert bytes(u.mem_read(heap, size)) == bytes(u.mem_read(src, size))
    if rc == 0:
        assert bytes(u.mem_read(dest, len(expected_payload))) == expected_payload
        assert struct.unpack("<I", u.mem_read(count, 4))[0] == len(expected_payload)
        tail_length = 128 - len(expected_payload)
        assert bytes(u.mem_read(dest + len(expected_payload), tail_length)) == b"\xA5" * tail_length
    else:
        assert bytes(u.mem_read(dest, 128)) == b"\xA5" * 128
        assert struct.unpack("<I", u.mem_read(count, 4))[0] == capacity
    return {"case": name, "gsp_status": rc, "temporary_allocations": allocations,
            "selected_source": "KernelFsp descriptor" if primary_selected else "startup FRTS",
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
    fsp_hals = [fsp_hal_case(binary, chip, variant)
                for chip in (61, 62, 64, 65, 70, 71) for variant in range(4)]
    readers = [reader_case(binary, "valid"), reader_case(binary, "unaligned", offset=41),
               reader_case(binary, "bad_magic", magic=b"NOPE"),
               reader_case(binary, "offset_beyond", offset=129),
               reader_case(binary, "length_beyond", length=129),
               reader_case(binary, "capacity_short", capacity=31),
               reader_case(binary, "combined_overrun", offset=120),
               reader_case(binary, "zero_length", length=0),
               reader_case(binary, "header_overlap", offset=8)]
    readers += [reader_case(binary, f"null_{index}", null_arg=index) for index in (0, 2, 3)]
    sources = [source_case(binary, "primary_precedes_valid_fallback"),
               source_case(binary, "primary_only", fallback=False),
               source_case(binary, "descriptor_field_zero", descriptor_field=0),
               source_case(binary, "absent_primary", present=False),
               source_case(binary, "disabled_primary", enabled=False),
               source_case(binary, "disabled_primary_without_descriptor", enabled=False, descriptor=False),
               source_case(binary, "missing_descriptor_no_fallback", descriptor=False),
               source_case(binary, "zero_size_no_fallback", size=0),
               source_case(binary, "bad_primary_magic_no_fallback", magic=b"NOPE"),
               source_case(binary, "primary_capacity_short_no_fallback", capacity=1),
               source_case(binary, "no_sources", present=False, fallback=False),
               source_case(binary, "disabled_primary_no_fallback", enabled=False, fallback=False)]
    for n in range(40):
        try:
            extract_rom((b"GFWI" + bytes(36))[:n])
        except ValueError:
            pass
        else:
            raise AssertionError("Truncated header accepted")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"elf_sha256": ELF_SHA256,
        "scope": "Original constructor, full ROM reader, class-lookup fragment and GFWI "
                 "copy helper; dynamic casts, allocation/free and logs stubbed. "
                 "Synthetic memory only; no physical ROM read or platform-source acquisition.",
        "hal_cases": hals, "fsp_hal_cases": fsp_hals,
        "reader_cases": readers, "source_cases": sources}, indent=2) + "\n")
    print(f"PASS: {len(hals)} HAL selections, {len(readers)} GFWI cases, "
          f"{len(sources)} source-selection cases, {len(fsp_hals)} KernelFsp "
          "constructor cases and 40 short headers")


if __name__ == "__main__":
    main()
