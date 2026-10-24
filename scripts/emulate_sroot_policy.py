#!/usr/bin/env python3
"""Original registry-bound initialization and timer-abort flag, with OS stubs."""
import hashlib
import json
import struct
import pefile
from unicorn import UC_HOOK_CODE
from unicorn import arm64_const as regs
from test_sroot_send import ROOT, BASE, HASH
from emulate_sroot_image import machine, call, CTX, REQUEST, TABLE, STUB, STACK

DIRECTORY = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594'
INF_HASH = 'ea6f08cd6a3df63f8423c11cbb5175ab794bc670c4bf5d7b6e825df5d30eaa40'


def bounds(pe, lower=0xe0000000, upper=0xffffffff, missing=False):
    u, calls = machine(pe), []
    frame = STACK+0x4000
    for offset in (0x40, 0x50, 0x60, 0x70):
        u.mem_write(STUB+offset, bytes.fromhex('c0035fd6'))
    for offset, target in ((0x650, 0x40), (0x180, 0x50), (0x758, 0x60), (0x738, 0x70)):
        u.mem_write(TABLE+offset, struct.pack('<Q', STUB+target))

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address in (BASE+0x30704, BASE+0x309d0):
            u.emu_stop()
        elif address == STUB+0x40:
            u.reg_write(regs.UC_ARM64_REG_X0, CTX)
        elif address == STUB+0x50:
            assert reg(1) == REQUEST and reg(2) == 1 and reg(3) == 0x20019 and reg(4) == 0
            calls.append('open_device_hardware_key')
            u.mem_write(reg(5), struct.pack('<Q', 0x7777))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x60:
            descriptor = bytes(u.mem_read(reg(2), 16))
            length = struct.unpack_from('<H', descriptor)[0]
            text = bytes(u.mem_read(struct.unpack_from('<Q', descriptor, 8)[0], length)).decode('utf-16le')
            assert reg(1) == 0x7777 and reg(3) == 4
            assert text in ('DmdAddrStart', 'DmdAddrEnd')
            calls.append(text)
            if missing and text == 'DmdAddrStart':
                u.reg_write(regs.UC_ARM64_REG_X0, 0xc0000034)
            else:
                u.mem_write(reg(4), struct.pack('<I', lower if text == 'DmdAddrStart' else upper))
                u.mem_write(reg(5), struct.pack('<I', 4))
                u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x70:
            assert reg(1) == 0x7777
            calls.append('close_key')

    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in ((20, BASE+0x1b358), (21, BASE+0x1b328), (22, REQUEST), (26, frame)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.emu_start(BASE+0x30538, 0, count=100000)
    endpoint = u.reg_read(regs.UC_ARM64_REG_PC)-BASE
    values = struct.unpack('<III', u.mem_read(CTX+0x28, 12))
    return endpoint, calls, values


def timer(pe):
    u, calls = machine(pe), []
    for offset in (0x40, 0x50, 0x60):
        u.mem_write(STUB+offset, bytes.fromhex('c0035fd6'))
    for offset, target in ((0xa08, 0x40), (0x650, 0x50)):
        u.mem_write(TABLE+offset, struct.pack('<Q', STUB+target))

    def hook(_, address, size, user):
        if address == STUB+0x40:
            calls.append('WdfTimerGetParentObject')
            u.reg_write(regs.UC_ARM64_REG_X0, REQUEST)
        elif address == STUB+0x50:
            calls.append('get_device_context')
            u.reg_write(regs.UC_ARM64_REG_X0, CTX)
        elif address == STUB+0x60:
            assert u.reg_read(regs.UC_ARM64_REG_X1) == 0x1234
            due_argument = u.reg_read(regs.UC_ARM64_REG_X2)
            assert due_argument == (-700000000 & 0xffffffffffffffff)
            calls.append('WdfTimerStart_70_seconds')

    u.hook_add(UC_HOOK_CODE, hook)
    assert call(u, 0x3f80, (CTX,)) == 0
    call(u, 0x3fe0, (0x1234,))
    assert bytes(u.mem_read(CTX+0x18, 1)) == b'\x01'
    assert call(u, 0x3f80, (CTX,)) == 1
    assert call(u, 0x3f80, (0,)) == 1
    assert calls == ['WdfTimerGetParentObject', 'get_device_context']
    due = struct.unpack('<q', pe.get_data(0x321c0, 8))[0]
    assert due == -700000000
    u.mem_write(CTX+8, struct.pack('<Q', 0x1234))
    u.mem_write(TABLE+0x9f8, struct.pack('<Q', STUB+0x60))
    u.reg_write(regs.UC_ARM64_REG_X19, CTX)
    u.emu_start(BASE+0x31ddc, BASE+0x31e0c, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x31e0c
    assert calls[-1] == 'WdfTimerStart_70_seconds'
    # Original poll executes original predicate: flag aborts only while not ready.
    u.mem_write(CTX+0x38, struct.pack('<Q', CTX))
    u.mem_write(CTX+0x58, struct.pack('<Q', REQUEST+0x8000))
    assert call(u, 0x4828, (CTX, 32, 0, 0, BASE+0x3f80, CTX)) == 6
    u.mem_write(REQUEST+0x8020, struct.pack('<I', 1))
    assert call(u, 0x4828, (CTX, 32, 0, 0, BASE+0x3f80, CTX)) == 0
    return {'timer_callback_rva': '0x3fe0', 'predicate_rva': '0x3f80',
            'timer_due_100ns': due, 'relative_seconds': -due/10000000}


def main():
    assert hashlib.sha256((DIRECTORY/'nvsrootmbx.sys').read_bytes()).hexdigest() == HASH
    raw = (DIRECTORY/'nvsrootmbx.inf').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == INF_HASH
    text = raw.decode('utf-16le') if raw.startswith(b'\xff\xfe') else raw.decode()
    assert 'HKR,,DmdAddrStart,0x10001,0xe0000000' in text
    assert 'HKR,,DmdAddrEnd,0x10001,0xffffffff' in text
    pe = pefile.PE(str(DIRECTORY/'nvsrootmbx.sys'), fast_load=True)
    endpoint, calls, values = bounds(pe)
    assert endpoint == 0x30704 and values == (0xe0000000, 0xffffffff, 0x1fffffff)
    assert calls == ['open_device_hardware_key', 'DmdAddrStart', 'close_key',
                     'open_device_hardware_key', 'DmdAddrEnd', 'close_key']
    assert bounds(pe, lower=0xffffffff, upper=0xe0000000)[0] == 0x309d0
    endpoint, calls, _ = bounds(pe, missing=True)
    assert endpoint == 0x309d0 and calls == ['open_device_hardware_key', 'DmdAddrStart', 'close_key']
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'dma_minimum': '0xe0000000', 'dma_maximum': '0xffffffff',
                      'stored_length_limit': '0x1fffffff', 'timer': timer(pe),
                      'limitations': ['Synthetic registry and timer dispatch; no live Windows registry read',
                                      'Timer flag bounds wait-ready, not the separate pending-drain loops',
                                      'No Linux IOMMU/domain configuration or DMA quiescence proof']}, indent=2))


if __name__ == '__main__':
    main()
