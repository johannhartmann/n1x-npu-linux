#!/usr/bin/env python3
"""Candidate RISC-V AUIPC+ADDI references in the extracted monitor code.

Two-byte scanning accounts for compressed instructions. Matches still require
manual disassembly; register lifetime is not established by this scanner.
"""
import argparse
import json
import struct
from pathlib import Path


def signed(value, bits):
    return value-(1 << bits) if value & (1 << (bits-1)) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('code', type=Path)
    parser.add_argument('strings', nargs='+')
    parser.add_argument('--base', type=lambda x: int(x, 0), default=0x100000)
    args = parser.parse_args()
    code = args.code.read_bytes()
    targets = {}
    for name in args.strings:
        offset = code.find(name.encode())
        if offset < 0:
            parser.error('missing string: '+name)
        targets[args.base+offset] = name
    rows = []
    for offset in range(0, len(code)-4, 2):
        ins = struct.unpack_from('<I', code, offset)[0]
        if ins & 0x7f != 0x17:
            continue
        reg = (ins >> 7) & 31
        page = args.base+offset+signed(ins & 0xfffff000, 32)
        for delta in range(4, 34, 2):
            if offset+delta+4 > len(code):
                break
            nxt = struct.unpack_from('<I', code, offset+delta)[0]
            if nxt & 0x707f != 0x13 or (nxt >> 15) & 31 != reg:
                continue
            target = page+signed(nxt >> 20, 12)
            if target in targets:
                rows.append({'string': targets[target], 'string_va': hex(target),
                             'auipc_va': hex(args.base+offset),
                             'addi_va': hex(args.base+offset+delta), 'register': reg})
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
