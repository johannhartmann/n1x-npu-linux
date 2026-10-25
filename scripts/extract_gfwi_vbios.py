#!/usr/bin/env python3
"""Extract a ROM from an already acquired GFWI file; no hardware access.

The pinned GSP reader uses header u32s at +0x20/+0x24 as offset/length.
This tool additionally checks their combined extent. Output is not proof
that the ROM belongs to the P16 or contains the NPU's BIT-l policy.
"""
import argparse
import hashlib
import struct
from pathlib import Path

MAX_ROM_SIZE = 0x700000  # ROM capacity selected by the pinned GB20B HAL.


def extract_rom(container):
    if len(container) < 40 or container[:4] != b"GFWI":
        raise ValueError("Missing or truncated GFWI header")
    offset, size = struct.unpack_from("<II", container, 32)
    if offset < 40 or not 0 < size <= MAX_ROM_SIZE:
        raise ValueError("Invalid ROM offset or length")
    if offset > len(container) or size > len(container) - offset:
        raise ValueError("ROM extends beyond the GFWI container")
    return bytes(container[offset:offset + size])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", type=Path, help="File starting with GFWI")
    parser.add_argument("output", type=Path, help="New output file; never overwritten")
    args = parser.parse_args()
    try:
        rom = extract_rom(args.container.read_bytes())
        with args.output.open("xb") as output:
            output.write(rom)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Extracted {len(rom)} bytes; SHA256 {hashlib.sha256(rom).hexdigest()}")
    print("Board identity and NPU BIT-l policy remain to be verified.")


if __name__ == "__main__":
    main()
