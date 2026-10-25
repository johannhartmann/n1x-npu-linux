#!/usr/bin/env python3
"""Execute original IRQ mask policy, forwarding thunks and HAL-1 accessors in RAM."""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256
from emulate_profile1_irq_init import run as firmware_init, PROFILE1_HASH

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, QUEUE, VTABLE, STACK, STUB = (0x27000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


def run(pe, enable, saved, current, owned, read_error=0, write_error=0, capture_route=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd48000, 0x3000), (0xd3c000, 0x1000), (0x641000, 0x1000),
                      (0xd43000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address, size in ((BASE+0xd57000, 0x1000), (BASE+0xd38000, 0x2000),
                          (BASE+0xe3e000, 0x1000), (CTX, 0x10000),
                          (QUEUE, 0x10000), (VTABLE, 0x10000),
                          (STACK, 0x10000), (STUB, 0x10000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd38758, 0xd39878, 0xd39f38):
        u.mem_write(BASE+rva, ret)
    u.mem_write(STUB, ret)
    for address, value in ((BASE+0xe3eaf0, STUB), (CTX+0x68, QUEUE),
                           (QUEUE, VTABLE), (QUEUE-8+0x298, BASE+0xd4a8e0),
                           (QUEUE-8+0x2a0, BASE+0xd48820),
                           (QUEUE-8+0x2b0, BASE+0xd4a8d0),
                           (QUEUE-8+0x2a8, BASE+0xd48810),
                           (CTX+0xd8, BASE+0xd49800)):
        u.mem_write(address, struct.pack('<Q', value))
    for slot in (0xa0, 0xb8, 0xc0, 0xc8):
        u.mem_write(VTABLE+slot, pe.get_data(0x108e9b8+slot, 8))
    u.mem_write(CTX+0x49c, struct.pack('<I', saved))
    u.mem_write(QUEUE+0x54, struct.pack('<I', 0xa5a5a5a5 if capture_route else owned))
    trace = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39878:
            assert reg(1) == 0 and reg(2) in (0xd28, 0xd2c)
            value = owned if reg(2) == 0xd2c else current
            trace.append(['read', reg(2), value])
            u.mem_write(reg(3), struct.pack('<I', value))
            u.reg_write(regs.UC_ARM64_REG_X0, read_error)
        elif address == BASE+0xd39f38:
            assert reg(1) == 0 and reg(2) in (0xd20, 0xd24)
            trace.append(['write', reg(2), reg(3)])
            u.reg_write(regs.UC_ARM64_REG_X0, write_error)
        elif address == BASE+0xd38758:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0x12340000, CTX, enable)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    end = BASE+0xd43c98 if capture_route else STOP
    if capture_route:
        for i, value in ((20, CTX), (21, 0x12340000), (24, QUEUE)):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.emu_start(BASE+0xd43c48 if capture_route else BASE+0xd49800, end, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == end
    return {'status': reg(0), 'saved': struct.unpack('<I', u.mem_read(CTX+0x49c, 4))[0],
            'trace': trace, 'owned': struct.unpack('<I', u.mem_read(QUEUE+0x54, 4))[0]}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for saved in (0, 1, 0x12345678, 0xffffffff):
        for enable in (0, 1, 0x100, 0x101):
            for current, owned in ((0, 0xc0), (0xffffffff, 0xc0), (0x12345678, 0xf0)):
                for read_error, write_error in ((0, 0), (0x65, 0), (0, 0x65)):
                    result = run(pe, enable, saved, current, owned, read_error, write_error)
                    if enable & 255:
                        expected_saved = saved | 0xc0
                        expected_trace = [['write', 0xd20, expected_saved]]
                    elif read_error:
                        expected_saved = saved
                        expected_trace = [['read', 0xd28, current]]
                    else:
                        value = current & owned
                        expected_saved = value if value else saved
                        expected_trace = [['read', 0xd28, current], ['write', 0xd24, value]]
                    assert result == {'status': 0, 'saved': expected_saved, 'trace': expected_trace, 'owned': owned}, result
                    rows.append(result)
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    initialized = {offset: value for offset, size, value in firmware_init(blob)['writes']}
    routing = []
    for route, current in ((initialized[0xd2c], initialized[0xd20]), (0xc0, 0xffffffff), (0, 0xffffffff)):
        result = run(pe, 0, 0x12340000, current, route, capture_route=True)
        value = route & current
        assert result['owned'] == route
        assert result['saved'] == (value if value else 0x12340000)
        assert result['trace'] == [['read', 0xd2c, route], ['read', 0xd28, current],
                                   ['write', 0xd24, value]]
        routing.append({'route': hex(route), 'mask': hex(current), 'clear': hex(value)})
    print(json.dumps({'passed': True, 'cases': len(rows), 'entry': '0xd49800',
                      'hardware_access': False, 'enable_bits': '0xc0',
                      'boot_route_capture_cases': routing,
                      'limitations': ['Synthetic HAL fields and register values',
                                      'No physical IRQ routing or complete host IRQ lifecycle established',
                                      'Windows suppresses accessor errors; Linux must propagate them']}, indent=2))


if __name__ == '__main__':
    main()
