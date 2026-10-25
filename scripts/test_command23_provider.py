#!/usr/bin/env python3
"""Execute the Windows setup prefix that defaults the command-23 provider.

Registry access, logging and OS callbacks are explicit synthetic boundaries.
Stops before the next setup helper; does not claim full device initialization.
"""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from test_command23_builder import windows, BASE, ROOT, KMD_SHA256
from arm64_stack_cookie import install as install_cookie

DEVICE, PROVIDER, CTX, OUTER, STACK, STUB = (0x31000000+i*0x10000 for i in range(6))


def execute(pe, initial):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva in (0xd44000, 0xd39000, 0xd3b000, 0xd57000, 0xe3e000,
                0x108f000, 0x1512000, 0x13ac000, 0xce000):
        u.mem_map(BASE+rva, 0x1000)
        u.mem_write(BASE+rva, pe.get_data(rva, 0x1000))
    for address in (DEVICE, PROVIDER, CTX, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6')*2)
    u.mem_write(BASE+0x1512148, struct.pack('<Q', DEVICE))
    u.mem_write(DEVICE+0x78, struct.pack('<Q', PROVIDER))
    u.mem_write(PROVIDER+0xf4, struct.pack('<I', initial))
    u.mem_write(CTX+0x128, struct.pack('<Q', STUB))
    u.mem_write(CTX+0x410, struct.pack('<Q', STUB))
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB+4))
    # Suppress logging so the default branch cannot depend on logging behavior.
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))
    stopped = False
    queries = 0
    trace = []

    def value(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        nonlocal stopped, queries
        trace.append(hex(address))
        if address == STUB:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == BASE+0xd4444c:
            assert value(23) == PROVIDER
            stopped = True
            u.emu_stop()
        elif address == BASE+0xd39160:
            assert value(0) == DEVICE
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
            u.reg_write(regs.UC_ARM64_REG_PC, value(30))
        elif address == BASE+0xd3b8c0:
            queries += 1
            assert value(0) == OUTER
            # Configuration is absent. Preserve the caller's initialized output.
            u.reg_write(regs.UC_ARM64_REG_X0, 1)
            u.reg_write(regs.UC_ARM64_REG_PC, value(30))

    u.reg_write(regs.UC_ARM64_REG_X0, DEVICE)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    handle = u.hook_add(UC_HOOK_CODE, hook)
    try:
        u.emu_start(BASE+0xd440d0, BASE+0xd45000, count=10000)
    except Exception as error:
        raise RuntimeError(f"PC={u.reg_read(regs.UC_ARM64_REG_PC):#x}, trace={trace[-12:]}") from error
    u.hook_del(handle)
    assert stopped and queries > 0
    return struct.unpack('<I', u.mem_read(PROVIDER+0xf4, 4))[0]


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for initial in (0, 1, 4, 19200, 0xffffffff):
        provider = execute(pe, initial)
        assert provider == (initial or 4)
        result = windows(pe, 1, 1, provider)
        assert result['status'] == 0
        record = result['record']
        assert struct.unpack_from('<I', record, 2)[0] == (provider*2000)&0xffffffff
        rows.append({'initial': initial, 'provider_after_setup': provider,
                     'command23_delay_field': struct.unpack_from('<I', record, 2)[0]})
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'setup_prefix': ['0xd440d0', '0xd4444c'],
                      'limitations': ['Registry and OS callbacks are synthetic; configuration queries fail',
                                      'Stops before the subsequent setup helper, not a complete initialization',
                                      'Existing nonzero provider values are retained, not measured on Windows',
                                      'Delay field 8000 is the zero-input fallback, not a measured P16 setting or physical timing calibration']}, indent=2))


if __name__ == '__main__':
    main()
