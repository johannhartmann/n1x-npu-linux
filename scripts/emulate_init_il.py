#!/usr/bin/env python3
"""Run the diagnostic INIT_IL through the original table and opcode handlers.

Engine registers are RAM only. Register values are synthetic readiness inputs;
no NPU state or hardware register is accessed.
"""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE, UC_HOOK_MEM_READ
from unicorn import riscv_const as regs
from emulate_firmware_commands import ROOT, BLOB_HASH
from inspect_loadable import Buffer

ENGINE, CACHE, STACK, STOP = 0x10019180000, 0x184400, 0x20100000, 0x20200000


def run(blob, instructions, count, ready=True):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for address, size in ((0x100000, 0xb000), (0x180000, 0x10000),
                          (ENGINE, 0x10000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[0x200:0xb100])
    u.mem_write(0x180000, blob[0xb100:])
    u.mem_write(0x101e9a, bytes.fromhex('67800000'))
    reads, handlers = [], []

    def code(_, address, size, user):
        if address == 0x101e9a:
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address == 0x102d2a:
            handlers.append(hex(address))

    def read(_, access, address, size, value, user):
        if ENGINE <= address < ENGINE+0x10000:
            reads.append({'offset': hex(address-ENGINE), 'bytes': size})
            # Hook runs before the actual RAM read.
            u.mem_write(address, (1 if ready else 0).to_bytes(size, 'little'))

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_READ, read)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x10316e, STOP, count=1000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    table = {str(index): hex(struct.unpack('<Q', u.mem_read(0x188a20+index*8, 8))[0])
             for index in range(256) if struct.unpack('<Q', u.mem_read(0x188a20+index*8, 8))[0]}
    u.mem_write(CACHE, bytes(instructions))
    u.reg_write(regs.UC_RISCV_REG_A0, STACK+0x1000)
    u.reg_write(regs.UC_RISCV_REG_A1, count)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x102d60, STOP, count=1000)
    status = u.reg_read(regs.UC_RISCV_REG_A0)
    return {'returned': u.reg_read(regs.UC_RISCV_REG_PC) == STOP,
            'pc': hex(u.reg_read(regs.UC_RISCV_REG_PC)),
            'status': status-(1 << 64) if status >= 1 << 63 else status,
            'register_reads': reads, 'handlers': handlers, 'installed_handlers': table}


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == '51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543'
    b = Buffer(data)
    for table in b.tables(b.u('<I', 0), 5):
        if b.string(table, 0) == 'task0-ib':
            vector = b.vector(table, 5, 1)
            instructions = data[vector.start+122880:vector.start+122928]
            break
    else:
        raise ValueError('missing diagnostic instructions')
    success = run(blob, instructions, 3)
    assert success['returned'] and success['status'] == 0
    assert success['register_reads'] == [{'offset': hex(x), 'bytes': 4} for x in (0x7008, 0x5000, 0x6000)]
    assert success['handlers'] == ['0x102d2a']*3
    waiting = run(blob, instructions, 3, ready=False)
    assert not waiting['returned'] and len(waiting['handlers']) == 1
    assert len(waiting['register_reads']) > 10
    assert all(item['offset'] == '0x7008' for item in waiting['register_reads'])
    bad_subtype = bytearray(instructions[:16])
    bad_subtype[1] = 0
    rejected = run(blob, bad_subtype, 1)
    assert rejected['returned'] and rejected['status'] == -3 and not rejected['register_reads']
    unknown = run(blob, bytes(16), 1)
    assert unknown['returned'] and unknown['status'] == -3 and not unknown['register_reads']
    print(json.dumps({'passed': True, 'hardware_access': False, 'firmware_sha256': BLOB_HASH,
                      'scope': 'Original table initialization, IL batch dispatcher and poll handler with RAM registers',
                      'engine_internal_base': hex(ENGINE), 'instruction_hex': instructions.hex(),
                      'rejected_subtype_case': rejected, 'ready_case': success, 'not_ready_case': waiting, 'unknown_opcode_case': unknown,
                      'limitations': ['Ready bits are synthetic, not observed NPU status',
                                      'No instruction-fetch DMA or complete task execution in this test',
                                      'Observation budget exhaustion is not a firmware timeout',
                                      'Internal firmware addresses do not identify host MMIO mappings']}, indent=2))


if __name__ == '__main__':
    main()
