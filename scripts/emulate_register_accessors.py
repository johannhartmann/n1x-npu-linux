#!/usr/bin/env python3
"""Resolve Windows HAL accessor offsets by executing only tiny ARM64 wrappers.

Uses the original HAL initializer and RAM-only read/write stubs. Resource index
zero is a Windows resource selector; no physical base is assumed here.
"""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, OUT, STOP = 0x20000000, 0x20100000, 0x20200000
READ, WRITE = BASE+0xd39878, BASE+0xd39f38
SENTINEL = 0x12345678


def decode(pe, selector=2):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0x1951000, 0xd000), (BASE+0xd48000, 0x3000),
                       (BASE+0xd39000, 0x1000), (CTX, 0x2000),
                       (OUT, 0x1000), (STOP, 0x1000)):
        uc.mem_map(addr, size)
    uc.mem_write(BASE+0x1951000, pe.get_data(0x1951000, 0xd000))
    uc.mem_write(BASE+0xd48000, pe.get_data(0xd48000, 0x3000))
    uc.mem_write(READ, bytes.fromhex('c0035fd6'))
    uc.mem_write(WRITE, bytes.fromhex('c0035fd6'))
    uc.mem_write(CTX+0x496, struct.pack('<H', selector))
    uc.reg_write(regs.UC_ARM64_REG_X0, CTX)
    uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
    uc.emu_start(BASE+0x1951238, STOP, count=50000)
    assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP
    rows, trace = [], []

    def hook(_, address, size, user):
        if address not in (READ, WRITE):
            return
        resource = uc.reg_read(regs.UC_ARM64_REG_X1)
        offset = uc.reg_read(regs.UC_ARM64_REG_X2)
        value = uc.reg_read(regs.UC_ARM64_REG_X3)
        if address == READ:
            assert value == OUT
            uc.mem_write(OUT, struct.pack('<I', SENTINEL))
        trace.append({'operation': 'read' if address == READ else 'write',
                      'resource_index': resource, 'offset': hex(offset),
                      'value': SENTINEL if address == READ else value})
        uc.reg_write(regs.UC_ARM64_REG_X0, 0x42)

    uc.hook_add(UC_HOOK_CODE, hook)
    for slot in range(0x220, 0x3d0, 8):
        fn = struct.unpack('<Q', uc.mem_read(CTX+slot, 8))[0]
        indexed = slot < 0x280
        for index in ((0, 1, 3) if indexed else (0,)):
            trace.clear()
            uc.mem_write(OUT, struct.pack('<I', SENTINEL))
            for reg, value in ((regs.UC_ARM64_REG_X0, 0x11223344),
                               (regs.UC_ARM64_REG_X1, CTX),
                               (regs.UC_ARM64_REG_X2, index if indexed else OUT),
                               (regs.UC_ARM64_REG_X3, OUT), (regs.UC_ARM64_REG_LR, STOP)):
                uc.reg_write(reg, value)
            uc.emu_start(fn, STOP, count=30)
            assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP
            assert uc.reg_read(regs.UC_ARM64_REG_X0) == 0x42
            assert len(trace) == 1 and trace[0]['resource_index'] == 0
            assert trace[0]['value'] == SENTINEL
            rows.append({'hal_slot': hex(slot), 'accessor_rva': hex(fn-BASE),
                         'index': index, **trace[0]})
    return rows


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    rows = decode(pefile.PE(str(path), fast_load=True))
    # Independent reference: L4T R38.4 non-firewall RISC-V register header.
    expected = {0x220: 0x180, 0x230: 0x184, 0x240: 0x188,
                0x250: 0x1c0, 0x260: 0x1c4, 0x2f8: 0x444,
                0x300: 0x444, 0x308: 0x10c, 0x310: 0x10c,
                0x318: 0x110, 0x320: 0x110, 0x328: 0x114,
                0x330: 0x114, 0x338: 0x11c, 0x340: 0x11c,
                0x348: 0x118, 0x350: 0x118, 0x358: 0xb88, 0x360: 0xb88}
    for slot, offset in expected.items():
        row = next(r for r in rows if r['hal_slot'] == hex(slot) and r['index'] == 0)
        assert int(row['offset'], 16) == offset, (hex(slot), row)
    print(json.dumps({'scope': 'RAM-only original Windows accessors; selector 2; no hardware',
                      'physical_resource_mapping': 'not yet established',
                      'verified_reference_offsets': len(expected), 'accessors': rows}, indent=2))


if __name__ == '__main__':
    main()
