#!/usr/bin/env python3
"""Execute the complete original SROOT reinit handler with a RAM mailbox.

KMDF objects, timer scheduling and mailbox responses are modeled. No device is
opened and no firmware command is sent to physical hardware.
"""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ
from unicorn import arm64_const as regs

from test_sroot_exchange import Bus
from test_sroot_full_upload import (CTX, DEVICE, REQUEST, MEMORY, INPUT, BUFFER,
                                    TABLE, STUB, STACK, MMIO, STOP)
from test_sroot_send import ROOT, BASE, HASH


def run(pe, request, *, header=0x1a, busy=0, input_error=0,
        output_error=0xc0000023, null_input=False, diagnostic=0x12345678):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    image = pe.get_memory_mapped_image()
    u.mem_map(BASE, (len(image)+4095) & ~4095)
    u.mem_write(BASE, image)
    for address in (CTX, DEVICE, REQUEST, MEMORY, INPUT, BUFFER,
                    TABLE, STUB, STACK, MMIO):
        u.mem_map(address, 0x20000)
    u.mem_write(INPUT, request or b'\0')
    u.mem_write(BUFFER, b'\xa5'*16)
    u.mem_write(STACK, b'\xa5'*0x20000)
    bus = Bus(16, header, [], busy=busy)

    def put(address, fmt, *values):
        u.mem_write(address, struct.pack('<'+fmt, *values))

    names = ('cfg', 'context', 'lock', 'timer_start', 'input_memory',
             'memory_buffer', 'output_memory', 'timer_stop', 'unlock')
    stubs = {STUB+i*16: name for i, name in enumerate(names)}
    for ptr in stubs:
        u.mem_write(ptr, bytes.fromhex('c0035fd6'))
    for ptr, value in ((BASE+0x1b358, BASE+0x1b358), (BASE+0x1b838, TABLE),
                       (BASE+0x19200, STUB), (CTX+8, 0x1234),
                       (CTX+0x10, 0x5678), (CTX+0x58, MMIO)):
        put(ptr, 'Q', value)
    for offset, name in ((0x650, 'context'), (0x9c8, 'lock'),
                         (0x9f8, 'timer_start'), (0x858, 'input_memory'),
                         (0x610, 'memory_buffer'), (0x860, 'output_memory'),
                         (0xa00, 'timer_stop'), (0x9d0, 'unlock')):
        put(TABLE+offset, 'Q', STUB+names.index(name)*16)
    put(CTX+0x50, 'I', bus.stride)
    put(CTX+0x20, 'I', diagnostic)
    calls, entries, sent = [], [], []
    exchange = {}
    observed = {0x31760, 0x37f0, 0x5580, 0x54c0, 0x5130, 0x5328,
                0x4f70, 0x3f80, 0x31290}

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, pc, size, user):
        if pc-BASE in observed:
            entries.append(hex(pc-BASE))
        if pc == BASE+0x5580:
            raw = bytes(u.mem_read(reg(0), 72))
            assert raw[8] == 0x1a and raw[0x18] == 3 and raw[0x28] == 0
            assert struct.unpack_from('<Q', raw, 0x30)[0] == BASE+0x3f80
            ptr, = struct.unpack_from('<Q', raw, 0x10)
            sent.append(list(struct.unpack('<III', u.mem_read(ptr, 12))))
            exchange['status_pointer'], = struct.unpack_from('<Q', raw, 0x40)
        if pc == BASE+0x31a8c:
            exchange['transport_status'] = reg(0) & 0xffffffff
            exchange['firmware_status'], = struct.unpack('<I', u.mem_read(
                exchange.pop('status_pointer'), 4))
        name = stubs.get(pc)
        if name is None or name == 'cfg':
            return
        calls.append(name)
        result = 0
        if name == 'context':
            assert reg(1) == DEVICE
            result = CTX
        elif name == 'lock':
            assert (reg(1), reg(2)) == (0x5678, 0)
        elif name == 'timer_start':
            assert (reg(1), reg(2)) == (0x1234, (-700000000) & ((1 << 64)-1))
        elif name in ('input_memory', 'output_memory'):
            assert reg(1) == REQUEST
            result = input_error if name == 'input_memory' else output_error
            if not result:
                put(reg(2), 'Q', MEMORY if name == 'input_memory' else MEMORY+16)
        elif name == 'memory_buffer':
            assert reg(1) in (MEMORY, MEMORY+16)
            is_input = reg(1) == MEMORY
            put(reg(2), 'Q', len(request) if is_input else 4)
            result = (0 if null_input else INPUT) if is_input else BUFFER
        elif name == 'timer_stop':
            assert (reg(1), reg(2)) == (0x1234, 1)
        elif name == 'unlock':
            assert reg(1) == 0x5678
        else:
            raise AssertionError(name)
        u.reg_write(regs.UC_ARM64_REG_X0, result)

    def memory(_, access, address, size, value, user):
        assert size == 4
        if access == UC_MEM_READ:
            put(address, 'I', bus.read(address-MMIO))
        else:
            bus.write(address-MMIO, value)

    u.hook_add(UC_HOOK_CODE, hook)
    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory,
               begin=MMIO, end=MMIO+0xfff)
    for i, value in enumerate((DEVICE, REQUEST)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x10000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x31760, STOP, count=300000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert u.reg_read(regs.UC_ARM64_REG_SP) == STACK+0x10000
    assert bytes(u.mem_read(INPUT, len(request))) == request
    assert calls[-2:] == ['timer_stop', 'unlock']
    return {'status': reg(0) & 0xffffffff, 'request': request.hex(),
            'reply_header': hex(header), 'calls': calls, 'entries': entries,
            'sent': sent, 'tx': bus.tx, 'mmio': bus.trace, 'exchange': exchange,
            'output': bytes(u.mem_read(BUFFER, 4)).hex()}


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for mode in (None, 1, 2):
        request = struct.pack('<I', 0xc0302)+(struct.pack('<I', mode) if mode else b'')
        for header in (0x1a, 0x101a, 0x8000001a, 0x77):
            row = run(pe, request, header=header)
            assert row['sent'] == [[0xc0302, 0, mode or 1]]
            assert row['tx'] == [0x31a, *row['sent'][0]]
            assert row['status'] == (0 if header in (0x1a, 0x77) else 0xc0000229)
            assert row['exchange'] == {'transport_status': 0,
                'firmware_status': ((header >> 12) & 4095) | ((header >> 24) << 12)}
            assert row['entries'] == ['0x31760', '0x37f0', '0x5580', '0x54c0',
                                      '0x5130', '0x5328', '0x4f70', '0x31290']
            assert row['calls'] == ['context', 'lock', 'timer_start', 'input_memory',
                                   'memory_buffer', 'output_memory', 'timer_stop', 'unlock']
            assert row['output'] == 'a5a5a5a5'
            rows.append(row)
    request = bytes.fromhex('02030c0002000000')
    for size in (0, 1, 3, 5, 7, 9, 12):
        row = run(pe, (request+bytes(12))[:size])
        assert not row['sent'] and not row['mmio']
        assert row['status'] == (0 if size > 8 else 0xc000000d)
        rows.append(row)
    for kwargs, status in (({'null_input': True}, 0),
                            ({'input_error': 0xc0000023}, 0xc0000023),
                            ({'busy': 1}, 0xc0000229),
                            ({'output_error': 0}, 0)):
        row = run(pe, request, **kwargs)
        assert row['status'] == status
        if kwargs.get('null_input') or kwargs.get('input_error'):
            assert not row['sent'] and not row['mmio']
        rows.append({'injected': kwargs, **row})
    for header in (0x1a, 0x101a, 0x8000001a):
        row = run(pe, request, header=header, output_error=0)
        assert row['output'] == '78563412'
        assert row['status'] == (0 if header == 0x1a else 0xc0000229)
        rows.append({'injected': {'output_error': 0}, **row})
    print(json.dumps({'passed': True, 'hardware_access': False,
        'driver_sha256': HASH, 'handler': '0x31760', 'cases': rows,
        'limitations': ['WDF calls, timer scheduling and all mailbox responses are synthetic',
            'Success does not establish secure-firmware behavior or DMA quiescence',
            'Oversized/null-buffer cases are handler-level fixtures, not claims about public IOCTL reachability',
            'No native recovery or physical reinitialization is enabled']}, indent=2))


if __name__ == '__main__':
    main()
