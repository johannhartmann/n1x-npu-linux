#!/usr/bin/env python3
"""Decode the pinned component-9 diagnostic file; never access hardware.

The scratch interpretation uses the public GB100 FRTS layout. Its applicability
and the address translation on GB20B still require a matching container.
"""
import argparse
import hashlib
import json
from pathlib import Path


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Varint exceeds uint64")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("Unterminated varint")


def fields(data):
    offset, result = 0, {}
    while offset < len(data):
        key, offset = varint(data, offset)
        number, wire = key >> 3, key & 7
        if not 0 < number < 1 << 29:
            raise ValueError("Invalid field number")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise ValueError("Truncated field")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("Unsupported wire type")
        result.setdefault(number, []).append((wire, value))
    return result


def single(message, number, wire):
    values = message.get(number, [])
    if len(values) != 1 or values[0][0] != wire:
        raise ValueError(f"Expected one field {number} of wire type {wire}")
    return values[0][1]


def decode(data):
    if not 0 < len(data) <= 1024 * 1024:
        raise ValueError("Diagnostic file exceeds bound or is empty")
    gpu = fields(single(fields(data), 3, 2))
    vbios = fields(single(gpu, 111, 2))
    registers = fields(single(vbios, 12, 2))
    if single(registers, 1, 0) != 1 or single(registers, 2, 0) != 0x1400:
        raise ValueError("Unexpected register space/base")
    packed = single(registers, 4, 2)
    words, offset = [], 0
    while offset < len(packed):
        word, offset = varint(packed, offset)
        if word > 0xffffffff or len(words) == 64:
            raise ValueError("Scratch dump exceeds 64 uint32s")
        words.append(word)
    if len(words) != 64:
        raise ValueError("Expected exactly 64 scratch registers")
    config = words[63]
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "raw_rom_field_present": 10 in vbios,
        "scratch_words": [hex(word) for word in words],
        "candidate_frts": {
            "layout_source": "GB100 dev_bus_addendum.h; GB20B semantics unverified",
            "address": hex(words[61] | words[62] << 32),
            "size": (config & 0xffff) << 12,
            "media": "sysmem" if config & 0x10000 else "fb",
            "unknown_config_bits": hex(config & ~0x1ffff),
            "cpu_physical_mapping_verified": False,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(decode(args.capture.read_bytes()), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
