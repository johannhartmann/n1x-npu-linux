#!/usr/bin/env python3
"""Execute original selector resolver with explicit, synthetic provider bytes."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH, CTX, OUT, STACK, STOP, call


def run(pe, byte, *, cached=None, provider_status=0, missing=None):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0x75000, 0x2b000, 0xa5000, 0x6a1000, 0x6a5000):
        u.mem_map(BASE+page, 4096)
        u.mem_write(BASE+page, pe.get_data(page, 4096))
    for address in (CTX, OUT, STACK, STOP):
        u.mem_map(address, 0x10000)
    u.mem_write(CTX+0x98, struct.pack('<I', 0x7fffffff))
    u.mem_write(OUT, struct.pack('<Q', OUT+0x100))
    u.mem_write(OUT+0x118, struct.pack('<Q', STOP+0x20))
    u.mem_write(OUT+0x1b8, struct.pack('<Q', STOP+0x40))
    u.mem_write(BASE+0xa5340, struct.pack('<Q', STOP+0x60))
    if cached is not None:
        u.mem_write(BASE+0x6a5950, b'\x01')
        u.mem_write(BASE+0x6a1c30, struct.pack('<I', cached))
    events = []

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    def hook(_, address, size, user):
        if address in (BASE+0x2bb00, BASE+0x2bfb0):
            name = 'first-lookup' if address == BASE+0x2bb00 else 'second-lookup'
            events.append(name)
            ret(0 if missing == name else OUT)
        elif address == STOP+0x20:
            events.append('provider-lookup')
            ret(0 if missing == 'provider-lookup' else OUT)
        elif address == STOP+0x40:
            events.append('provider-query')
            target = u.reg_read(regs.UC_ARM64_REG_X1)
            assert bytes(u.mem_read(target, 4)) == bytes(4)
            u.mem_write(target, bytes([byte]))
            ret(provider_status)
        elif address == STOP+0x60:
            u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    status = call(u, 0x756f8, CTX+0x98)
    selector = struct.unpack('<I', u.mem_read(CTX+0x98, 4))[0]
    valid = u.mem_read(BASE+0x6a5950, 1)[0]
    global_value = struct.unpack('<I', u.mem_read(BASE+0x6a1c30, 4))[0]
    return dict(byte=byte, cached=cached, missing=missing, provider_status=provider_status,
                status=status, selector=selector, cache_valid=valid,
                cache_value=global_value, events=events)


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(path), fast_load=True)
    # init2 passes the selector address directly to the resolver.
    assert pe.get_data(0x1c2a8, 12) == bytes.fromhex('60620291682211a912650194')
    rows = []
    for byte in range(256):
        row = run(pe, byte)
        expected = {0x23: 1, 0x25: 3, 0x26: 3}.get(byte)
        assert row['status'] == (0 if expected is not None else 15)
        assert row['selector'] == (expected if expected is not None else 0x7fffffff)
        assert row['cache_valid'] == (expected is not None)
        if expected is not None:
            assert row['cache_value'] == expected
        rows.append(row)
    for cached in (0, 1, 2, 3, 0x7fffffff, 0xffffffff):
        row = run(pe, 0, cached=cached)
        assert row['status'] == 0 and row['selector'] == cached and not row['events']
        rows.append(row)
    for missing, status in [('first-lookup', 6), ('second-lookup', 11), ('provider-lookup', 11)]:
        row = run(pe, 0x23, missing=missing)
        assert row['status'] == status and row['selector'] == 0x7fffffff and not row['cache_valid']
        rows.append(row)
    row = run(pe, 0x23, provider_status=0x65)
    assert row['status'] == 0 and row['selector'] == 1
    rows.append(row)
    print(json.dumps(dict(passed=True, hardware_access=False, resolver='0x756f8',
                         cases=len(rows), accepted_byte_mappings={'0x23': 1, '0x25': 3, '0x26': 3},
                         observations=['Provider query return status is not checked before interpreting its byte',
                                       'Cached values are copied without range validation'],
                         limitations=['Provider object and callback are modeled; actual physical provider byte is unknown',
                                      'No device boot or model execution; byte semantics and KMD connection unresolved'],
                         special_cases=rows[256:]), indent=2))


if __name__ == '__main__':
    main()
