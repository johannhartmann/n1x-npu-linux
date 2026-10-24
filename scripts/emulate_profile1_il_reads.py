#!/usr/bin/env python3
"""Measure P16 RV32 IL dispatch DMA extents; opcode execution is stubbed."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_il_reads import ROOT, HOST, STACK, STOP

CTX, TASK = 0x20300000, 0x20301000


def run(blob, count):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (HOST, 0x20000), (CTX, 0x2000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    u.mem_write(CTX, struct.pack('<I', TASK))
    for address in (0x101c96, 0x104ed8, 0x102abe):
        u.mem_write(address, bytes.fromhex('67800000'))
    reads, batches = [], []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}'))

    def hook(_, address, size, user):
        if address == 0x104ed8:
            source = reg(0) | reg(1) << 32
            destination, length = reg(2), reg(3)
            assert HOST <= source and source+length <= HOST+0x20000 and length == 256
            reads.append(source-HOST)
            u.mem_write(destination, bytes(u.mem_read(source, length)))
        elif address == 0x102abe:
            assert reg(0) == CTX
            batches.append(reg(1))
        if address in (0x101c96, 0x104ed8, 0x102abe):
            u.reg_write(regs.UC_RISCV_REG_A0, 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((CTX, HOST, 0, count)):
        u.reg_write(getattr(regs, f'UC_RISCV_REG_A{i}'), value)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x102b68, STOP, count=10000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP and reg(0) == 0
    return {'read_offsets': reads, 'instruction_batches': batches, 'dma_extent': max(reads)+256}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for count in (1, 3, 15, 16, 17, 31, 32, 33, 48, 64):
        result = run(blob, count)
        blocks = 1 if count <= 16 else count//16+1
        assert result['read_offsets'] == list(range(0, blocks*256, 256))
        assert sum(result['instruction_batches']) == count
        rows.append({'count': count, **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'architecture': 'RV32',
                      'firmware_sha256': PROFILE1_HASH, 'entry': '0x102b68', 'cases': rows,
                      'limitations': ['Synthetic engine/task context, DMA and opcode execution',
                                      'Read extents only; IL slice decoding and opcode semantics remain separate',
                                      'No physical DMA, scheduling or inference']}, indent=2))


if __name__ == '__main__':
    main()
