#!/usr/bin/env python3
"""Run the pinned Windows DMA transfer with synthetic RAM, IOVA and callbacks."""
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
OBJ, VTABLE, FW, HEADER, STACK, STUBS, DMA = (
    0x20000000, 0x20100000, 0x20200000, 0x20300000,
    0x20400000, 0x20500000, 0x20600000)
IOVA = 0x30000003  # Deliberately unaligned synthetic bus address.
STOP = STUBS+0xff0
RET = bytes.fromhex('c0035fd6')
SLOTS = (0x100, 0x108, 0x118, 0x128, 0x138, 0x148, 0x150, 0x158, 0xf8)


def run(pe, blob, fail_slot=None, busy=False):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0xd4b000, 0x2000), (BASE+0xd57000, 0x1000),
                       (BASE+0xd39000, 0x1000), (BASE+0xd3b000, 0x1000),
                       (BASE+0xcc000, 0x3000), (BASE+0xe3e000, 0x1000),
                       (BASE+0x1512000, 0x1000), (OBJ, 0x1000),
                       (VTABLE, 0x1000), (FW, 0x20000), (HEADER, 0x1000),
                       (STACK, 0x10000), (STUBS, 0x1000), (DMA, 0x20000)):
        uc.mem_map(addr, size)
    uc.mem_write(BASE+0xd4bf80, pe.get_data(0xd4bf80, 0x6bc))
    helpers = (0xce4b0, 0xd39160, 0xd3b0f8,
               0xcd148, 0xccee8, 0xd3b298, 0xd3b328, 0xd4cad8)
    for rva in helpers:
        uc.mem_write(BASE+rva, RET)
    uc.mem_write(STUBS+0x800, RET)
    uc.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUBS+0x800))
    for slot in SLOTS:
        uc.mem_write(VTABLE+slot, struct.pack('<Q', STUBS+slot))
        uc.mem_write(STUBS+slot, RET)
    uc.mem_write(OBJ, struct.pack('<Q', VTABLE))
    uc.mem_write(FW, blob[0x200:])
    uc.mem_write(HEADER, struct.pack('<6I', 0, 0, 0, 0xaf00, 0xaf00, 0xa400))
    calls, copies, freed, tail = [], [], [], []
    allocated = []

    def reg(n):
        return uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))

    def result(value=0):
        uc.reg_write(regs.UC_ARM64_REG_X0, value)

    def hook(_, address, size, user):
        slot = address-STUBS
        if slot in SLOTS:
            ptr = reg(2)
            if slot == 0x100:
                uc.mem_write(ptr, struct.pack('<I', 0x201))
            elif slot == 0x150:
                uc.mem_write(ptr, struct.pack('<I', 1 if busy else 2))
            calls.append((slot, struct.unpack('<I', uc.mem_read(ptr, 4))[0]))
            result(0x65 if slot == fail_slot else 0)
        elif address == BASE+0xd39160:
            result(0x123)
        elif address == BASE+0xd3b0f8:
            allocated.append(reg(2))
            assert reg(2) <= 0x20000
            uc.mem_write(reg(3), struct.pack('<Q', DMA))
            uc.mem_write(reg(4), struct.pack('<Q', IOVA))
            result()
        elif address == BASE+0xcd148:
            assert reg(0) == DMA and reg(2) == allocated[0]
            uc.mem_write(reg(0), bytes([reg(1)&255])*reg(2))
            result(reg(0))
        elif address == BASE+0xccee8:
            assert reg(3) <= reg(1) and DMA <= reg(0) < DMA+0x20000
            copies.append((reg(0), reg(2), reg(3)))
            uc.mem_write(reg(0), bytes(uc.mem_read(reg(2), reg(3))))
            result()
        elif address == BASE+0xd3b328:
            freed.append((reg(2), reg(3)))
            result()
        elif address == BASE+0xd3b298:
            result()
        elif address == BASE+0xd4cad8:
            tail.append((reg(1), reg(2), reg(3), reg(4)))
            result()

    install_stack_cookie(uc, pe)
    uc.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0, OBJ, FW, HEADER)):
        uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
    uc.emu_start(BASE+0xd4bf80, STOP, count=2000000)
    assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP, 'instruction budget exhausted'
    return uc.reg_read(regs.UC_ARM64_REG_X0), calls, copies, freed, tail, allocated, uc


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == '8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75'
    status, calls, copies, freed, tail, allocated, uc = run(pe, blob)
    assert status == 0 and allocated == [0x16000]
    assert copies == [(DMA+0xfd, FW, 0xaf00), (DMA+0xb0fd, FW+0xaf00, 0xa400)]
    assert bytes(uc.mem_read(DMA+0xfd, 0xaf00)) == blob[0x200:0xb100]
    assert bytes(uc.mem_read(DMA+0xb0fd, 0xa400)) == blob[0xb100:]
    assert freed == [(DMA, IOVA)] and tail == [(OBJ, FW, HEADER, 0)]
    commands = [value for slot, value in calls if slot == 0x158]
    assert commands == [0x2610]*175+[0x2600]*164
    bases = [value for slot, value in calls if slot == 0x128]
    assert bases == [(IOVA+0xfd)>>8, (IOVA+0xb0fd)>>8]
    offsets = [value for slot, value in calls if slot == 0x138]
    assert offsets == list(range(0, 0xaf00, 256))+list(range(0, 0xa400, 256))
    failures = []
    for slot in SLOTS:
        code, trace, _, freed_case, tail_case, allocation, _ = run(pe, blob, slot)
        assert code == 0x65 and trace[-1][0] == slot and not tail_case
        assert freed_case == ([(DMA, IOVA)] if allocation else [])
        failures.append(hex(slot))
    code, trace, _, freed_case, tail_case, _, _ = run(pe, blob, busy=True)
    assert code == 0x65 and freed_case == [(DMA, IOVA)] and not tail_case
    assert sum(slot == 0x150 for slot, _ in trace) == 5001
    print(json.dumps({
        'scope': 'Offline original ARM64; synthetic allocator, bus address and callback responses',
        'entry_rva': '0xd4bf80', 'status': status, 'callback_count': len(calls),
        'allocation_bytes': allocated[0], 'code_dma_commands': 175, 'data_dma_commands': 164,
        'code_command': '0x2610', 'data_command': '0x2600',
        'block_bytes': 256, 'unaligned_iova_padding_bytes': 253,
        'failure_propagation_and_cleanup_slots': failures,
        'busy_poll_limit': 5001,
        'limitations': ['No DMA or device access occurs',
                        'Physical register mapping and actual firmware boot remain unverified',
                        'Readback verification is stubbed']}, indent=2))


if __name__ == '__main__':
    main()
