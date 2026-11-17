#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay the plaintext DLA firmware's HFRP-related bandwidth request in RAM.

This is a client-side request path, not a replay of the HFRP controller/server.
Only the pinned optional variant is accepted. No firmware is installed or loaded
onto hardware. Unicorn models all registers with private process memory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs

from prepare_inputs import FIRMWARE, ROOT

ENTRY, MMIO, STACK, STOP = 0x1060c6, 0x1400000, 0x20100000, 0x20200000
BUSY, LIMITS = 0x1862b0, 0x1862a8


def payload(data):
    # Decode the same signed native section envelope validated by the driver.
    prefix = 4 + 3 + int.from_bytes(data[5:7], 'big')
    native = data[prefix:]
    assert struct.unpack_from('<3I', native) == (1, 2, 0xefbeadde)
    section = struct.unpack_from('<8I', native, 12 + 32)
    assert section[:2] == (0xefbeadde, 0xc0302)
    start = section[3] + section[4]
    blob = native[start:start + section[5]]
    assert len(blob) == section[5]
    magic, version, size, header, offset, length = struct.unpack_from('<6I', blob)
    assert (magic, version, size) == (0x10fe, 1, len(blob))
    assert offset + length == size
    data_offset, data_size, code_offset, code_size = struct.unpack_from('<4I', blob, header + 0x28)
    assert (code_offset, code_size, data_offset, data_size) == (0, 0x9000, 0x9000, 0x7300)
    return blob[offset + code_offset:offset + code_offset + code_size], blob[offset + data_offset:]


def replay(code, data, *, bandwidth, timeout=1, busy=None):
    uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x8000),
                          (MMIO, 0x2000), (STACK, 0x10000), (STOP, 0x1000)):
        uc.mem_map(address, size)
    uc.mem_write(0x100000, code)
    uc.mem_write(0x180000, data)
    uc.mem_write(LIMITS, struct.pack('<II', 1000, 100))
    uc.mem_write(BUSY, bytes(6))
    if busy is not None:
        uc.mem_write(BUSY + busy, b'\1')
    writes, logs = [], []

    def hook(_, address, size, user):
        if address == 0x101c96:
            pointer = uc.reg_read(regs.UC_RISCV_REG_A2)
            logs.append(bytes(uc.mem_read(pointer, 96)).split(b'\0')[0].decode('ascii'))
        elif address != 0x1073ae:
            return
        # Logging and the critical-section entry are the only code stubs.
        uc.reg_write(regs.UC_RISCV_REG_A0, 0)
        uc.reg_write(regs.UC_RISCV_REG_PC, uc.reg_read(regs.UC_RISCV_REG_RA))

    def write(_, access, address, size, value, user):
        if MMIO <= address < MMIO + 0x2000:
            assert size == 4
            writes.append((address, value))

    uc.hook_add(UC_HOOK_CODE, hook)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    for register, value in ((regs.UC_RISCV_REG_A0, bandwidth), (regs.UC_RISCV_REG_A1, timeout),
                            (regs.UC_RISCV_REG_SP, STACK + 0x8000), (regs.UC_RISCV_REG_RA, STOP)):
        uc.reg_write(register, value)
    uc.emu_start(ENTRY, STOP, count=10000)
    assert uc.reg_read(regs.UC_RISCV_REG_PC) == STOP, 'Instruction budget exhausted'
    status = uc.reg_read(regs.UC_RISCV_REG_A0)
    if status & 0x80000000:
        status -= 1 << 32
    return {'bandwidth': bandwidth, 'timeout': timeout, 'busy_slot': busy,
            'status': status, 'logs': logs,
            'writes': [{'address': hex(address), 'value': hex(value)} for address, value in writes]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / FIRMWARE[1][0])
    args = parser.parse_args()
    digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
    if digest != FIRMWARE[1][3]:
        raise SystemExit('Unexpected plaintext variant SHA-256; no code executed')
    code, data = payload(args.input.read_bytes())
    cases = []
    for slot in range(6):
        result = replay(code, data, bandwidth=500, busy=slot)
        assert result['status'] == -12 and not result['writes']
        assert result['logs'] == ['HFRP is busy\n']
        cases.append(result)
    for bandwidth in (0, 100, 500, 1000, 2000, 0x1fffffff, 0x20000000):
        for timeout in (1, 0xffff, 0x10000):
            result = replay(code, data, bandwidth=bandwidth, timeout=timeout)
            expected = 0 if bandwidth < 0x20000000 and timeout < 0x10000 else -3
            assert result['status'] == expected, result
            if not expected:
                assert result['writes'][0] == {'address': '0x1400308',
                    'value': hex(0x60000000 | min(1000, max(100, bandwidth)))}
                assert result['writes'][-1] == {'address': '0x1400d40', 'value': '0x6'}
            else:
                assert not any(row['address'] == '0x1400d40' for row in result['writes'])
            cases.append(result)
    print(json.dumps({'input_sha256': digest, 'hardware_access': False,
                      'entry': hex(ENTRY), 'execution_mode': 'RV32', 'cases': cases,
                      'limitations': [
                          'Private RAM models all MMIO and busy/completion state',
                          'Logging and critical-section entry are modeled',
                          'The request recipient and its firmware image are not identified',
                          'An optional plaintext image is not evidence of production variant behavior',
                      ]}, indent=2))


if __name__ == '__main__':
    main()
