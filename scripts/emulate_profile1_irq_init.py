#!/usr/bin/env python3
"""Execute original HAL-1 RV32 interrupt initialization in synthetic RAM only."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT, MMIO, STACK, STOP


def run(blob, initial_control=0):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (MMIO, 0x40000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    u.mem_write(MMIO+0xf08, struct.pack('<I', initial_control))
    writes = []

    def write(_, access, address, size, value, user):
        if MMIO <= address < MMIO+0x40000:
            writes.append((address-MMIO, size, value))

    u.hook_add(UC_HOOK_MEM_WRITE, write)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x100370, STOP, count=100000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    return {'writes': writes, 'mstatus': u.reg_read(regs.UC_RISCV_REG_MSTATUS),
            'mie': u.reg_read(regs.UC_RISCV_REG_MIE),
            'mtvec': u.reg_read(regs.UC_RISCV_REG_MTVEC)}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for initial in (0, 0xffffffff, 0x12345678, 0x00c00000):
        result = run(blob, initial)
        assert len(result['writes']) == 41
        assert (result['mstatus'], result['mie'], result['mtvec']) == (0x2008, 0x888, 0x1002dc)
        # Pin the entire post-table sequence, including instruction order.
        expected = [(0xf08, (initial & 0xff3fffff) | 0x800000),
                    (0xe60, 0), (0x3f0, 0xffffffff), (0x3f4, 0xffffffff),
                    (0x3f8, 0xffffffff), (0x3fc, 0xffffffff),
                    (0x294, 0xffffffff), (0x80, 0x30000), (0x38, 0),
                    (0xd2c, 0x1d0), (0xd30, 0x100), (0xc, 0x1f2c),
                    (0xd20, 0x1f85), (0xd60, 0x3f), (0xd64, 0xffffffff),
                    (0xd4c, 0), (0xd54, 0x3f0fc3f), (0x40, 0), (0x44, 0)]
        assert result['writes'][22:] == [(offset, 4, value) for offset, value in expected]
        assert result['writes'][0] == (0xf20, 4, 4)
        table = [0xc0000807, 0xc0100811, 0xc0190819, 0xc0200821,
                 0x80240824, 0x80250825, 0x8c000880, 0xc34808cb,
                 0xc0cc08d1, 0x839708dc]
        expected_table = []
        for index, value in enumerate(table):
            expected_table.extend([(0xf24, 4, index), (0xf28, 4, value)])
        expected_table.append((0xf24, 4, 0x80000000))
        assert result['writes'][1:22] == expected_table
        rows.append({'initial_f08': hex(initial), 'writes': len(result['writes']),
                     'mstatus': hex(result['mstatus']), 'mie': hex(result['mie']),
                     'mtvec': hex(result['mtvec'])})
    print(json.dumps({'passed': True, 'entry': '0x100370', 'table_setup': '0x1009b6',
                      'firmware_sha256': PROFILE1_HASH, 'cases': rows,
                      'hardware_access': False, 'function_stubs': [],
                      'limitations': ['Synthetic zero-initialized MMIO and CPU CSR state',
                                      'Not the complete reset entry or firmware boot',
                                      'Register writes are observations, not a host initialization recipe',
                                      'Physical IRQ routing and ACPI GSI assignment remain unverified']}, indent=2))


if __name__ == '__main__':
    main()
