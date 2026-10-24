#!/usr/bin/env python3
"""Measure original IL dispatch DMA extents; instruction semantics are stubbed."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE
from unicorn import riscv_const as regs
from emulate_firmware_commands import ROOT, BLOB_HASH

HOST, STACK, STOP = 0x20000000, 0x20100000, 0x20200000


def run(blob, count):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for address, size in ((0x100000, 0xb000), (0x180000, 0x10000),
                          (HOST, 0x20000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[0x200:0xb100])
    u.mem_write(0x180000, blob[0xb100:])
    for address in (0x101e9a, 0x10543e, 0x102d60):
        u.mem_write(address, bytes.fromhex('67800000'))
    reads, batches = [], []

    def hook(_, address, size, user):
        if address == 0x10543e:
            source = u.reg_read(regs.UC_RISCV_REG_A0)
            destination = u.reg_read(regs.UC_RISCV_REG_A1)
            assert HOST <= source and source+256 <= HOST+0x20000
            reads.append(source-HOST)
            u.mem_write(destination, bytes(u.mem_read(source, 256)))
        elif address == 0x102d60:
            batches.append(u.reg_read(regs.UC_RISCV_REG_A1))
        if address in (0x101e9a, 0x10543e, 0x102d60):
            u.reg_write(regs.UC_RISCV_REG_A0, 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_RISCV_REG_A0, 0), (regs.UC_RISCV_REG_A1, HOST),
                            (regs.UC_RISCV_REG_A2, count), (regs.UC_RISCV_REG_SP, STACK+0x8000),
                            (regs.UC_RISCV_REG_RA, STOP)):
        u.reg_write(register, value)
    u.emu_start(0x102fde, STOP, count=10000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    assert u.reg_read(regs.UC_RISCV_REG_A0) == 0
    return {'read_offsets': reads, 'instruction_batches': batches, 'dma_extent': max(reads)+256}


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    rows = []
    for count in (1, 3, 15, 16, 17, 31, 32, 33, 48, 64):
        result = run(blob, count)
        expected_blocks = 1 if count <= 16 else count//16+1
        assert result['read_offsets'] == list(range(0, expected_blocks*256, 256))
        assert sum(result['instruction_batches']) == count
        rows.append({'count': count, **result})
    print(json.dumps({'passed': True, 'firmware_sha256': BLOB_HASH, 'hardware_access': False,
                      'entry': '0x102fde', 'instruction_bytes': 16, 'cases': rows,
                      'limitations': ['Instruction execution and DMA hardware are replaced by RAM stubs',
                                      'This proves read extents, not model validity or inference']}, indent=2))


if __name__ == '__main__':
    main()
