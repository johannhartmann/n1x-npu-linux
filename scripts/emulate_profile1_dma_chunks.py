#!/usr/bin/env python3
"""Run original RV32 DMA-read setup and observe lower-level transfer chunks."""
import hashlib
import json
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT, MMIO, STACK, STOP

DESTINATION = 0x184700


def run(blob, length, source=0x20002000, destination=DESTINATION, fail_chunk=None):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (MMIO, 0x2000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    # Busy query and final hardware-issue primitive are the only replaced calls;
    # original channel configuration, decomposition and CSR helpers execute.
    for address in (0x100262, 0x10021c, 0x101c96):
        u.mem_write(address, bytes.fromhex('67800000'))
    chunks = []

    def hook(_, pc, size, user):
        if pc == 0x10021c:
            a = [u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}')) for i in range(6)]
            assert a[4:] == [2, 1]
            chunks.append({'source': a[1] | a[2] << 32, 'destination': a[0], 'bytes': a[3]})
            u.reg_write(regs.UC_RISCV_REG_A0, 1 if len(chunks) == fail_chunk else 0)
        elif pc in (0x100262, 0x101c96):
            u.reg_write(regs.UC_RISCV_REG_A0, 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((source & 0xffffffff, source >> 32, destination, length, 0)):
        u.reg_write(getattr(regs, f'UC_RISCV_REG_A{i}'), value)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x104e0a, STOP, count=10000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    status = u.reg_read(regs.UC_RISCV_REG_A0)
    return status-(1 << 32) if status >= 1 << 31 else status, chunks


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for length in (0, 4, 8, 60, 72, 248, 256, 264, 504, 4096):
        for source in (0x20002000, 0x12ffffff00):
            status, chunks = run(blob, length, source)
            assert status == 0 and sum(c['bytes'] for c in chunks) == length
            position = 0
            for item in chunks:
                assert item['source'] == source+position and item['destination'] == DESTINATION+position
                assert item['bytes'] in (4, 8, 16, 32, 64, 128, 256)
                position += item['bytes']
            rows.append({'length': length, 'source': hex(source), 'chunks': chunks})
    for length in (1, 3, 6, 255):
        assert run(blob, length) == (-3, [])
    assert run(blob, 8, source=0x20002001) == (-3, [])
    assert run(blob, 8, destination=DESTINATION+1) == (-3, [])
    _, full = run(blob, 504)
    for index in range(1, len(full)+1):
        assert run(blob, 504, fail_chunk=index) == (-4, full[:index])
    print(json.dumps({'passed': True, 'hardware_access': False, 'architecture': 'RV32',
                      'original_setup': '0x104e0a', 'stubbed_issue_primitive': '0x10021c',
                      'cases': rows, 'chunk_failure_cases': len(full),
                      'limitations': ['Final hardware issue is stubbed; no physical DMA executed',
                                      'Only read setup/channel zero and aligned pointers are checked',
                                      'Observed chunk arguments do not establish IOMMU mapping or coherency']}, indent=2))


if __name__ == '__main__':
    main()
