#!/usr/bin/env python3
"""Run actual P16 HAL-1 RV32 postactions and caller with RAM-only DMA."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT, CMD, HOST, STACK, STOP
from emulate_profile1_commands import CTX


def run(blob, stream, declared_size=None, offset=0, fail_dma=None, descriptor=None, task_status=0):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (CMD, 0x2000), (CTX, 0x4000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    u.mem_write(0x1862bc, struct.pack('<I', CTX))
    for address in (0x101c96, 0x104ed8, 0x105004):
        u.mem_write(address, bytes.fromhex('67800000'))
    u.mem_write(CMD+offset, stream)
    if descriptor is not None:
        u.mem_write(CMD, bytes(descriptor))
        u.mem_write(STACK+0x1000, bytes(descriptor[:60]))
        u.mem_write(STACK+0x1080, struct.pack('<Q', CMD))
    u.mem_write(HOST, struct.pack('<I', 0xdeadbeef))
    transfers = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}'))

    def hook(_, pc, size, user):
        if pc == 0x101c96:
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif pc in (0x104ed8, 0x105004):
            if pc == 0x104ed8:
                source, destination = reg(0) | reg(1) << 32, reg(2)
                direction = 'read'
            else:
                source, destination = reg(0), reg(1) | reg(2) << 32
                direction = 'write'
            length = reg(3)
            transfers.append({'direction': direction, 'source': hex(source),
                              'destination': hex(destination), 'length': length})
            error = len(transfers) == fail_dma
            if descriptor is not None and direction == 'read':
                error = error or not (CMD <= source and source+length <= CMD+len(descriptor))
            if not error:
                u.mem_write(destination, bytes(u.mem_read(source, length)))
            u.reg_write(regs.UC_RISCV_REG_A0, 0xfffffffc if error else 0)

    u.hook_add(UC_HOOK_CODE, hook)
    args = (CMD+offset, 0, len(stream) if declared_size is None else declared_size, 0, 0, 0)
    entry = 0x1037ac
    if descriptor is not None:
        entry, args = 0x103d56, (STACK+0x1000, task_status & 0xffffffff, 0)
    for i, value in enumerate(args):
        u.reg_write(getattr(regs, f'UC_RISCV_REG_A{i}'), value)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(entry, STOP, count=10000)
    status = reg(0)
    if status >= 1 << 31:
        status -= 1 << 32
    return {'returned': u.reg_read(regs.UC_RISCV_REG_PC) == STOP,
            'status': status, 'host_word': struct.unpack('<I', u.mem_read(HOST, 4))[0],
            'transfers': transfers}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for offset in (0, 1, 2, 3, 250, 255):
        for value in (0, 1, 0x12345678, 0xffffffff):
            stream = b'\x80'+struct.pack('<QI', HOST, value)+b'\0'
            result = run(blob, stream, offset=offset)
            assert result['returned'] and result['status'] == 0 and result['host_word'] == value, result
            reads = 2 if offset+len(stream) > 256 else 1
            assert [t['direction'] for t in result['transfers']] == ['read']*reads+['write']
            assert [t['length'] for t in result['transfers']] == [256]*reads+[4]
            assert [int(t['source'], 16) for t in result['transfers'][:reads]] == [CMD+256*i for i in range(reads)]
            rows.append({'case': f'write_sem_offset_{offset}_value_{value:x}', **result})
    stream = b'\x80'+struct.pack('<QI', HOST, 0x12345678)+b'\0'
    for size in (13, 15):
        result = run(blob, stream, declared_size=size)
        assert result['returned'] and result['status'] == -7 and result['host_word'] == 0x12345678, result
        assert [t['direction'] for t in result['transfers']] == ['read', 'write']
        rows.append({'case': f'bad_list_size_{size}_still_writes_fence', **result})
    for index, status in ((1, -8), (2, -4)):
        result = run(blob, stream, fail_dma=index)
        assert result['returned'] and result['status'] == status and result['host_word'] == 0xdeadbeef, result
        assert len(result['transfers']) == index
        rows.append({'case': f'dma_failure_{index}', **result})
    result = run(blob, stream, offset=250, fail_dma=2)
    assert result['returned'] and result['status'] == -8 and result['host_word'] == 0xdeadbeef
    rows.append({'case': 'cross_cache_block_read_failure', **result})
    for opcode in (1, 0xff):
        result = run(blob, bytes([opcode])+bytes(31))
        assert result['returned'] and result['status'] == -7 and result['host_word'] == 0xdeadbeef
        assert len(result['transfers']) == 1
        rows.append({'case': f'invalid_opcode_{opcode:x}', **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'architecture': 'RV32',
                      'firmware_sha256': PROFILE1_HASH, 'interpreter': '0x1037ac',
                      'caller': '0x103d56', 'case_count': len(rows), 'cases': rows,
                      'limitations': ['Synthetic firmware context and DMA; logging is stubbed',
                                      'No task scheduling, hardware cache ordering or inference',
                                      'Aligned 256-byte cache fetches require mapped backing beyond the 14-byte stream']}, indent=2))


if __name__ == '__main__':
    main()
