#!/usr/bin/env python3
"""Run actual HAL-1 defaults, allocation preparation, provider and region-5 builder."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH

BASE = 0x140000000
CTX, DEVICE, PROVIDER, OUTER, STACK, STUB = (0x35000000+i*0x10000 for i in range(6))
RING, LPWR, CONFIG = 0x36000000, 0x36100000, 0x36101000
STOP = STUB+0xff0
ALLOC_BYTES, RING_OFFSET, ENTRY_BYTES = 1 << 20, 16, 52
RING_BYTES = ((ALLOC_BYTES-RING_OFFSET-8)//ENTRY_BYTES)*ENTRY_BYTES+8


def windows(pe, *, allocation_error=0, null_ring=False, send_error=0, disabled=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x1951000, 0xd000), (0xd43000, 0x1000), (0xd45000, 0x1000),
                      (0xd47000, 0x1000), (0xd49000, 0x1000), (0xd57000, 0x1000),
                      (0xd39000, 0x1000), (0xd38000, 0x1000), (0xd3b000, 0x1000),
                      (0xcd000, 0x1000), (0xcc000, 0x1000), (0xce000, 0x1000),
                      (0xe3e000, 0x1000), (0x1512000, 0x1000), (0x13ac000, 0x1000),
                      (0x61a000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, DEVICE, PROVIDER, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    u.mem_map(RING, ALLOC_BYTES)
    u.mem_map(LPWR, 8192)
    u.mem_write(RING, b'\xa5'*ALLOC_BYTES)
    u.mem_write(LPWR, b'\xa5'*8192)
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6')*4)
    for address, value in ((BASE+0xe3eaf0, STUB), (BASE+0x1512148, DEVICE),
                           (DEVICE+0x78, PROVIDER)):
        u.mem_write(address, struct.pack('<Q', value))
    # Prior test establishes 4 as the provider's zero-input fallback; this is
    # fixture input, not a measured live setting. LPWR allocation is incidental.
    u.mem_write(PROVIDER+0xf4, struct.pack('<I', 4))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))
    events, methods = [], []
    allocated = 0

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        nonlocal allocated
        if address in (BASE+0xd39160, BASE+0xd38a38):
            ret(OUTER)
        elif address == BASE+0xd3b0f8:
            count = reg(2)
            assert count == (ALLOC_BYTES if allocated == 0 and not disabled else 4096)
            if count == ALLOC_BYTES:
                cpu, dma, error = (0 if null_ring else RING), HOST, allocation_error
            else:
                cpu = LPWR if not allocated or (allocated == 1 and not disabled) else CONFIG
                dma, error = HOST+0x200000+(cpu-LPWR), 0
            if not error:
                u.mem_write(reg(3), struct.pack('<Q', cpu))
                u.mem_write(reg(4), struct.pack('<Q', dma))
            events.append(['allocate', count, cpu, dma, error])
            allocated += 1
            ret(error)
        elif address == BASE+0xcd148:
            u.mem_write(reg(0), bytes(reg(2)))
            events.append(['clear', reg(0), reg(2)])
            ret(reg(0))
        elif address == BASE+0xccee8:
            assert reg(3) in (16, 40)
            u.mem_write(reg(0), bytes(u.mem_read(reg(2), reg(3))))
            events.append(['copy', reg(3)])
            ret(0)
        elif address == BASE+0xd3b298:
            events.append(['cache', reg(1), reg(2), reg(3)])
            ret(0)
        elif address == STUB+4:
            assert [reg(i) for i in (1, 2, 3, 4, 5)] == [CTX, 11, HOST >> 8, 0, 0]
            methods.append([11, reg(3)])
            ret(send_error)
        elif address in (BASE+0xce440, BASE+0xce4b0, BASE+0xd38758):
            ret(0)

    u.hook_add(UC_HOOK_CODE, hook)

    def call(rva, *args):
        for i, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        u.emu_start(BASE+rva, STOP, count=50000)
        assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return reg(0)

    u.mem_write(CTX+0x496, struct.pack('<H', 1))
    call(0x1951238, CTX)
    callbacks = {hex(slot): hex(struct.unpack('<Q', u.mem_read(CTX+slot, 8))[0]-BASE)
                 for slot in (0x150, 0x158, 0x160, 0x440)}
    assert callbacks == {'0x150': '0xd45250', '0x158': '0xd470d0',
                         '0x160': '0xd49c20', '0x440': '0x61a7f0'}
    u.reg_write(regs.UC_ARM64_REG_X21, CTX)
    u.emu_start(BASE+0x195d154, BASE+0x195d1ac, count=100)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x195d1ac
    assert struct.unpack('<I', u.mem_read(CTX+0x480, 4))[0] == 1
    if disabled:
        u.mem_write(CTX+0x480, bytes(4))
    # Original +f8 method wrapper is outside this region-setup scope.
    u.mem_write(CTX+0xf8, struct.pack('<Q', STUB+4))
    prepared = call(0xd43730, DEVICE, CTX)
    pair = struct.unpack('<QQ', u.mem_read(CTX+0x4c8, 16))
    if not allocation_error and not null_ring and not disabled:
        assert pair == (RING, HOST) and bytes(u.mem_read(RING, ALLOC_BYTES)) == bytes(ALLOC_BYTES)
    status = call(0xd49c20, DEVICE, CTX, 5)
    return {'prepare_status': prepared, 'builder_status': status, 'callbacks': callbacks,
            'events': events, 'methods': methods, 'pair': pair,
            'record': bytes(u.mem_read(RING, 16)), 'payload': bytes(u.mem_read(RING, ALLOC_BYTES))}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    result = windows(pe)
    assert result['prepare_status'] == result['builder_status'] == 0
    expected = struct.pack('<IQI', 5, HOST+RING_OFFSET, RING_BYTES)
    assert result['record'] == expected and result['payload'] == expected+bytes(ALLOC_BYTES-16)
    assert result['methods'] == [[11, HOST >> 8]]
    assert RING_BYTES == 0xfffd8 and (RING_BYTES-8)//ENTRY_BYTES == 20164
    assert ALLOC_BYTES-RING_OFFSET-RING_BYTES == 24
    assert ['cache', RING, ALLOC_BYTES, 1] in result['events']
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    observed = []

    def response(u):
        observed.append((bytes(u.mem_read(0x1859d0, 16)), bytes(u.mem_read(0x1860f8, 8))))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    firmware = run(blob, method=0x30b, host_payload=result['payload'], host_handler=response,
                   boot_from_reset=True, via_trap=True, dma_register_model=True)
    assert firmware['returned'] and firmware['firmware_code_unmodified'] and not firmware['function_stubs']
    assert firmware['interrupts'] == [(0xb02, HOST >> 8)]
    assert observed == [(expected, struct.pack('<II', 8, 8))]
    assert bytes.fromhex(firmware['host_payload_final']) == result['payload']
    failures = []
    for args, status in (({'allocation_error': 0x65}, 0x40), ({'null_ring': True}, 0x40),
                          ({'disabled': True}, 0x6d), ({'send_error': 0x65}, 0x65)):
        failed = windows(pe, **args)
        assert failed['prepare_status'] == 0 and failed['builder_status'] == status
        assert bool(failed['methods']) == ('send_error' in args)
        failures.append({'injected': args, 'prepare_status': failed['prepare_status'],
                         'builder_status': status})
    print(json.dumps({'passed': True, 'hardware_access': False, 'callbacks': result['callbacks'],
                      'allocation_bytes': ALLOC_BYTES, 'record_bytes': 16, 'ring_offset': RING_OFFSET,
                      'ring_bytes': RING_BYTES, 'entry_bytes': ENTRY_BYTES, 'entry_capacity': 20164,
                      'unused_tail_bytes': 24, 'windows_events': result['events'],
                      'failure_cases': failures,
                      'limitations': ['Allocator, cache/copy and OS boundaries are synthetic',
                                      'Windows buffer preparation ignores the region allocator failure; Linux must propagate it',
                                      'The raw-firmware test requests a response; Windows method callback receives flags zero',
                                      'Only registration/local header normalization; no ring producer/consumer or physical inference',
                                      'Opaque SROOT firmware identity and actual hardware acceptance remain unverified']}, indent=2))


if __name__ == '__main__':
    main()
