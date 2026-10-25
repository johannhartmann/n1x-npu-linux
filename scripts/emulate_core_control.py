#!/usr/bin/env python3
"""Observe original core-select/start/wait behavior using synthetic MMIO."""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, VTABLE, STACK, STUB, STOP = 0x20000000, 0x20100000, 0x20200000, 0x20300000, 0x20300ff0


def run(pe, entry, busy_reads=0, read_error=False, write_error=False):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0xd48000, 0x2000), (BASE+0xd4c000, 0x1000),
                       (BASE+0xd39000, 0x1000), (BASE+0xd57000, 0x1000),
                       (BASE+0xd8000, 0x1000), (BASE+0xce000, 0x1000),
                       (BASE+0xe3e000, 0x1000), (BASE+0x13ac000, 0x1000),
                       (CTX, 0x1000), (VTABLE, 0x1000),
                       (STACK, 0x10000), (STUB, 0x1000)):
        uc.mem_map(addr, size)
    uc.mem_write(BASE+0xd48000, pe.get_data(0xd48000, 0x2000))
    uc.mem_write(BASE+0xd4c000, pe.get_data(0xd4c000, 0x1000))
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39878, 0xd39f38, 0xd82c0, 0xce4b0):
        uc.mem_write(BASE+rva, ret)
    uc.mem_write(STUB, ret)
    uc.mem_write(STUB+0x10, ret)
    uc.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    uc.mem_write(CTX+0x68, struct.pack('<Q', CTX+8))
    uc.mem_write(CTX+8, struct.pack('<Q', VTABLE))
    uc.mem_write(VTABLE+0x1d0, struct.pack('<Q', STUB+0x10))
    trace, polls, delays = [], [], []

    def reg(n):
        return uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39f38:
            trace.append(('write', reg(1), reg(2), reg(3)))
            uc.reg_write(regs.UC_ARM64_REG_X0, 0x65 if write_error else 0)
        elif address == BASE+0xd39878:
            trace.append(('read', reg(1), reg(2), 0x11))
            uc.mem_write(reg(3), struct.pack('<I', 0x11))
            uc.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x10:
            value = int(len(polls) < busy_reads)
            polls.append(value)
            uc.mem_write(reg(2), struct.pack('<I', value))
            uc.reg_write(regs.UC_ARM64_REG_X0, 0x65 if read_error else 0)
        elif address == BASE+0xd82c0:
            delays.append(reg(0))

    install_stack_cookie(uc, pe)
    uc.hook_add(UC_HOOK_CODE, hook)
    for regno, value in enumerate((0, CTX+8 if entry == 0xd4ce48 else CTX)):
        uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{regno}'), value)
    uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
    uc.emu_start(BASE+entry, STOP, count=1000000)
    assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return uc.reg_read(regs.UC_ARM64_REG_X0), trace, polls, delays


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    code, trace, polls, delays = run(pe, 0xd48c40)
    assert code == 0 and trace == [('write', 0, 0xb80, 0x100000),
                                  ('write', 0, 0xe68, 0x10), ('read', 0, 0xe68, 0x11)]
    cases = []
    for name, kwargs, status, count, delay_count in (
        ('idle_immediately', {}, 0, 1, 1),
        ('idle_after_three_busy_reads', {'busy_reads': 3}, 0, 4, 4),
        ('busy_timeout', {'busy_reads': 9999}, 0x65, 5001, 5000),
        ('read_error', {'read_error': True}, 0x65, 1, 0),
        ('write_error_is_ignored_by_windows', {'write_error': True}, 0, 1, 1)):
        code, trace, polls, delays = run(pe, 0xd49e90, **kwargs)
        assert code == status and trace == [('write', 0, 0xb88, 1)]
        assert len(polls) == count and delays == [100]*delay_count
        cases.append({'case': name, 'status': code, 'idle_reads': count,
                      'delay_calls': delay_count})
    print(json.dumps({'scope': 'Offline original ARM64; synthetic MMIO and delay callbacks',
                      'select_rva': '0xd48c40', 'start_rva': '0xd49e90',
                      'wait_rva': '0xd4ce48', 'start_write': {'offset': '0xb88', 'value': 1},
                      'poll_register': '0x4c (IDLESTATE; decoded separately from vtable)',
                      'cases': cases,
                      'limitations': ['Idle does not prove firmware protocol readiness',
                                      'Delay helper argument unit is not established',
                                      'No power, reset, firmware boot or MMIO is performed']}, indent=2))


if __name__ == '__main__':
    main()
