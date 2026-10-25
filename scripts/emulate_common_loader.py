#!/usr/bin/env python3
"""Execute original complete Windows loader with original constructor defaults.

HAL hardware/transport callbacks are recording stubs; no physical boot occurs.
The original method-2 resume orchestrator and idle-wait helper execute as code.
"""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
CTX, QUEUE, VTABLE, OUTER, STACK, STUB = (0x2a000000+i*0x10000 for i in range(6))
STOP = STUB+0xff0
SLOTS = (0x78, 0x88, 0x90, 0xd8, 0x138, 0x160, 0x190, 0x400, 0x408, 0x438)


def run(pe, resumed=False, force_upload=False, busy=0, error_slot=None,
        error=0x65, route_error=0, enable=1):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd43000, 0x11000), (0xd57000, 0x1000),
                      (0x1951000, 0xd000), (0x108e000, 0x2000),
                      (0x13ac000, 0x1000), (0xe3e000, 0x1000),
                      (0xce000, 0x1000), (0xd38000, 0x4000),
                      (0x238000, 0x1000), (0x1512000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (CTX, QUEUE, VTABLE, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    u.mem_write(STUB, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    for rva in (0xce440, 0xce478, 0xce4b0, 0xd82c0, 0xd39160,
                0xd38758, 0xd3bfd8, 0x2385c0):
        if rva == 0xd82c0:
            u.mem_map(BASE+0xd8000, 0x1000)
        u.mem_write(BASE+rva, ret)
    u.mem_write(CTX+0x496, struct.pack('<H', 1))
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x1951238, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    original = {hex(slot): hex(struct.unpack('<Q', u.mem_read(CTX+slot, 8))[0]-BASE)
                for slot in SLOTS+(0x420,)}
    # Actual contiguous constructor defaults, including enabled optional flags.
    u.reg_write(regs.UC_ARM64_REG_X21, CTX)
    u.emu_start(BASE+0x195d154, BASE+0x195d1ac, count=100)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x195d1ac
    defaults = {hex(offset): int.from_bytes(u.mem_read(CTX+offset, 1), 'little')
                for offset in (0x474, 0x47c, 0x47d, 0x47e, 0x486)}
    assert defaults == {'0x474': 1, '0x47c': 0, '0x47d': 1, '0x47e': 1, '0x486': 0}
    u.mem_write(CTX+0x460, struct.pack('<I', 2))
    u.mem_write(CTX+0x5b4, struct.pack('<I', 9))
    u.mem_write(CTX+0x47c, bytes([int(resumed)]))
    u.mem_write(CTX+0x486, bytes([int(force_upload)]))
    u.mem_write(CTX+0x68, struct.pack('<Q', QUEUE))
    u.mem_write(QUEUE, struct.pack('<Q', VTABLE))
    u.mem_write(QUEUE+0x50, struct.pack('<I', enable))
    u.mem_write(QUEUE+0x54, struct.pack('<I', 0xa5a5a5a5))
    for index, slot in enumerate(SLOTS):
        address = STUB+0x100+index*16
        u.mem_write(address, ret)
        u.mem_write(CTX+slot, struct.pack('<Q', address))
    for slot, offset in ((0xa0, 0x300), (0x1d0, 0x310)):
        u.mem_write(STUB+offset, ret)
        u.mem_write(VTABLE+slot, struct.pack('<Q', STUB+offset))
    events = []
    polls = 0

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        nonlocal polls
        if STUB+0x100 <= address < STUB+0x100+len(SLOTS)*16 and (address-STUB-0x100)%16 == 0:
            slot = SLOTS[(address-STUB-0x100)//16]
            assert reg(0) == OUTER and reg(1) == CTX
            row = {'slot': hex(slot), 'arg2': reg(2),
                   'loader_busy': int.from_bytes(u.mem_read(CTX+0x5ac, 1), 'little')}
            if slot == 0x400:
                assert [reg(i) for i in (2, 3, 4)] == [0, 0, 1]
            events.append(row)
            u.reg_write(regs.UC_ARM64_REG_X0, error if slot == error_slot else 0)
        elif address == STUB+0x300:
            assert reg(0) == OUTER and reg(1) == QUEUE and reg(2) == QUEUE+0x54
            events.append({'read': 'route', 'loader_busy': 1})
            if not route_error:
                u.mem_write(reg(2), struct.pack('<I', 0x1d0))
            u.reg_write(regs.UC_ARM64_REG_X0, route_error)
        elif address == STUB+0x310:
            assert reg(0) == OUTER and reg(1) == QUEUE
            value = int(polls < busy)
            polls += 1
            events.append({'read': 'idle', 'value': value})
            u.mem_write(reg(2), struct.pack('<I', value))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        elif address in (BASE+0xd38758, BASE+0xd3bfd8, BASE+0x2385c0):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X0, OUTER), (regs.UC_ARM64_REG_X1, CTX),
                            (regs.UC_ARM64_REG_SP, STACK+0x8000), (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(register, value)
    try:
        u.emu_start(BASE+0xd439c0, STOP, count=2000000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'events': events, 'defaults': defaults,
            'original_callbacks': original,
            'loaded': int.from_bytes(u.mem_read(CTX+0x47c, 1), 'little'),
            'busy': int.from_bytes(u.mem_read(CTX+0x5ac, 1), 'little'),
            'counter': int.from_bytes(u.mem_read(CTX+0x5b4, 4), 'little'),
            'owned': int.from_bytes(u.mem_read(QUEUE+0x54, 4), 'little')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    initial = ['0x408', 'idle', 'route', '0xd8', '0x138', '0x160',
               '0x78', '0x90', '0x88', '0x438', '0xd8']
    later = ['0x400', 'idle', '0x160', '0x88', '0x438', '0xd8']

    def check(name, kwargs, expected, status=0, busy=0, loaded=1):
        result = run(pe, **kwargs)
        assert result['status'] == status, (name, result)
        assert [e.get('slot', e.get('read')) for e in result['events']] == expected, (name, result)
        assert result['busy'] == busy and result['loaded'] == loaded, (name, result)
        assert result['counter'] == (9 if busy else 0), (name, result)
        rows.append({'case': name, 'inputs': kwargs, **result})
        return result

    baseline = check('initial_load', {}, initial)
    assert baseline['original_callbacks'] == {
        '0x78': '0xd46300', '0x88': '0xd49570', '0x90': '0xd47830',
        '0xd8': '0xd49800', '0x138': '0xd48110', '0x160': '0xd49c20',
        '0x190': '0xd49480', '0x400': '0xd49ef0', '0x408': '0xd45cb0',
        '0x438': '0xd496e0', '0x420': '0xd47430'}
    assert baseline['owned'] == 0x1d0
    assert [e['arg2'] for e in baseline['events'] if e.get('slot') == '0xd8'] == [0, 1]
    check('initial_force_flag', {'force_upload': True}, initial)
    check('later_reinit', {'resumed': True}, later)
    check('later_upload', {'resumed': True, 'force_upload': True}, ['0x408']+later[1:])
    off = check('initial_irq_disabled', {'enable': 0}, initial)
    assert [e['arg2'] for e in off['events'] if e.get('slot') == '0xd8'] == [0, 0]
    check('initial_delayed_idle', {'busy': 3}, ['0x408']+['idle']*4+initial[2:])
    check('initial_upload_error', {'error_slot': 0x408}, ['0x408', '0xd8'], 0x65, 1, 0)
    check('initial_ping_error', {'error_slot': 0x138}, initial[:5]+['0xd8'], 0x65, 1, 0)
    check('initial_idle_timeout', {'busy': 9999}, ['0x408']+['idle']*5001+['0xd8'], 0x65, 1, 0)
    route = check('route_read_error_ignored', {'route_error': 0x65}, initial)
    assert route['owned'] == 0xffffffff
    for slot in (0xd8, 0x160, 0x78, 0x90, 0x88, 0x438):
        check(f'callback_{slot:x}_error_ignored', {'error_slot': slot}, initial)
    check('later_reinit_error', {'resumed': True, 'error_slot': 0x400},
          ['0x400', '0xd8'], 0x65, 0, 1)
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'entry': '0xd439c0', 'cases': rows,
                      'limitations': [
                          'Original loader, initializer/defaults, method-2 orchestrator and idle helper execute',
                          'HAL transport, ping, route, IRQ-mask and optional callbacks are recording stubs',
                          'Initial/later selects context byte 0x47c; no live cold/resume invocation is observed',
                          'Constructor defaults are verified, installed Windows registry overrides are unknown',
                          'No physical boot, coherency, DMA completion or inference is established']}, indent=2))


if __name__ == '__main__':
    main()
