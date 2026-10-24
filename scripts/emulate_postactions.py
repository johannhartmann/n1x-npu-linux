#!/usr/bin/env python3
"""Execute the original T35x postaction interpreter with RAM-only DMA."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE
from unicorn import riscv_const as regs
from emulate_firmware_commands import ROOT, CMD, HOST, STACK, STOP
from emulate_task_header_checks import BLOB_HASH


def run(blob, stream, declared_size=None, offset=0, fail_dma=None):
    uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for address, size in ((0x100000, 0xb000), (0x180000, 0xb000),
                          (CMD, 0x2000), (STACK, 0x10000), (STOP, 0x1000)):
        uc.mem_map(address, size)
    uc.mem_write(0x100000, blob[0x200:0xb100])
    uc.mem_write(0x180000, blob[0xb100:])
    for address in (0x101e9a, 0x10559e, 0x105662):
        uc.mem_write(address, bytes.fromhex('67800000'))
    uc.mem_write(CMD+offset, stream)
    uc.mem_write(HOST, struct.pack('<I', 0xdeadbeef))
    transfers = []

    def hook(_, address, size, user):
        if address == 0x101e9a:
            uc.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address in (0x10559e, 0x105662):
            source = uc.reg_read(regs.UC_RISCV_REG_A0)
            destination = uc.reg_read(regs.UC_RISCV_REG_A1)
            length = uc.reg_read(regs.UC_RISCV_REG_A2)
            direction = 'read' if address == 0x10559e else 'write'
            transfers.append({'direction': direction, 'source': hex(source),
                              'destination': hex(destination), 'length': length})
            error = len(transfers) == fail_dma
            if not error:
                uc.mem_write(destination, bytes(uc.mem_read(source, length)))
            uc.reg_write(regs.UC_RISCV_REG_A0, (1 << 64)-4 if error else 0)

    uc.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_RISCV_REG_A0, CMD+offset),
                            (regs.UC_RISCV_REG_A1, len(stream) if declared_size is None else declared_size),
                            (regs.UC_RISCV_REG_A2, 0), (regs.UC_RISCV_REG_A3, 0),
                            (regs.UC_RISCV_REG_SP, STACK+0x8000), (regs.UC_RISCV_REG_RA, STOP)):
        uc.reg_write(register, value)
    uc.emu_start(0x103cd2, STOP, count=10000)
    status = uc.reg_read(regs.UC_RISCV_REG_A0)
    if status >= 1 << 63:
        status -= 1 << 64
    return {'returned': uc.reg_read(regs.UC_RISCV_REG_PC) == STOP,
            'status': status, 'host_word': struct.unpack('<I', uc.mem_read(HOST, 4))[0],
            'transfers': transfers}


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    rows = []
    for offset in range(4):
        for value in (0, 1, 0x12345678, 0xffffffff):
            stream = b'\x80'+struct.pack('<QI', HOST, value)+b'\0'
            result = run(blob, stream, offset=offset)
            assert result['returned'] and result['status'] == 0, result
            assert result['host_word'] == value, result
            assert [t['direction'] for t in result['transfers']] == ['read', 'read', 'write']
            assert [t['length'] for t in result['transfers']] == [16, 16, 4]
            rows.append({'case': f'write_sem_offset_{offset}_value_{value:x}', **result})
    stream = b'\x80'+struct.pack('<QI', HOST, 0x12345678)+b'\0'
    for size in (13, 15):
        result = run(blob, stream, declared_size=size)
        assert result['returned'] and result['status'] == -7
        # A fence value does not prove that subsequent list validation succeeds.
        assert result['host_word'] == 0x12345678
        assert [t['direction'] for t in result['transfers']] == ['read', 'write', 'read']
        rows.append({'case': f'bad_list_size_{size}_still_writes_fence', **result})
    for index, status in ((1, -8), (2, -8), (3, -4)):
        result = run(blob, stream, fail_dma=index)
        assert result['returned'] and result['status'] == status
        assert result['host_word'] == 0xdeadbeef
        assert len(result['transfers']) == index
        rows.append({'case': f'dma_failure_{index}', **result})
    for opcode in (1, 0xff):
        result = run(blob, bytes([opcode])+bytes(31))
        assert result['returned'] and result['status'] == -7
        assert result['host_word'] == 0xdeadbeef
        assert len(result['transfers']) == 1
        rows.append({'case': f'invalid_opcode_{opcode:x}', **result})
    print(json.dumps({'firmware_sha256': BLOB_HASH, 'entry': '0x103cd2',
                      'scope': 'Original WRITE_SEM postaction with RAM-copy DMA and stubbed logging',
                      'cases': rows, 'passed': True, 'hardware_access': False,
                      'limitations': ['Caller list traversal and task execution are not exercised',
                                      'DMA coherency and physical ordering are not modeled',
                                      '16-byte DMA reads extend beyond the 14-byte stream; backing memory is padded']}, indent=2))


if __name__ == '__main__':
    main()
