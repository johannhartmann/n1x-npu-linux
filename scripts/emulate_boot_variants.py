#!/usr/bin/env python3
"""Run the original HAL-1 upload selector through its auxiliary-request boundary."""
import hashlib
import json
import struct
import tempfile
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from extract_profile1_firmware import extract_boot_variant
from arm64_stack_cookie import install as install_stack_cookie
from emulate_boot_policy import run as policy
from emulate_firmware_commands import ROOT

BASE = 0x140000000
CTX, OUTER, STACK, STUB = (0x28000000+i*0x10000 for i in range(4))
STOP = STUB+0xff0


def run(pe, remapper, fail_requests=0, previous_success=False, initial_variant=0,
        request_error=0x65):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x1951000, 0xd000), (0xd45000, 0x2000),
                      (0x108f000, 0x1000), (0xd57000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address, size in ((BASE+0xd39000, 0x1000),
                          (BASE+0xd3b000, 0x1000), (BASE+0xce000, 0x1000),
                          (BASE+0xe3e000, 0x1000), (BASE+0x1512000, 0x1000),
                          (BASE+0x13ac000, 0x1000), (CTX, 0x10000),
                          (OUTER, 0x10000), (STACK, 0x10000), (STUB, 0x10000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160, 0xd3bfd8, 0xce440, 0xce478, 0xce4b0):
        u.mem_write(BASE+rva, ret)
    for offset in (0, 0x10, 0x20):
        u.mem_write(STUB+offset, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(CTX+0x496, struct.pack('<H', 1))
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x1951238, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert struct.unpack('<Q', u.mem_read(CTX+0x3e8, 8))[0] == BASE+0xd46780
    assert struct.unpack('<Q', u.mem_read(CTX+0x458, 8))[0] == BASE+0xd46760
    u.mem_write(CTX+0x400, struct.pack('<Q', STUB+0x10))
    u.mem_write(OUTER+0x140, struct.pack('<Q', STUB+0x20))
    u.mem_write(CTX+0x478, struct.pack('<I', remapper))
    u.mem_write(CTX+0x488, struct.pack('<I', initial_variant))
    u.mem_write(CTX+0x48c, bytes([int(previous_success)]))
    requests = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        elif address == STUB+0x20:
            assert reg(0) == OUTER
            u.reg_write(regs.UC_ARM64_REG_X0, 0x254)
        elif address == STUB+0x10:
            assert [reg(i) for i in (0, 1, 4)] == [OUTER, CTX, 0]
            rva, length = reg(2)-BASE, reg(3)
            payload = pe.get_data(rva, length)
            assert len(payload) == length
            requests.append({'rva': hex(rva), 'bytes': length,
                             'sha256': hashlib.sha256(payload).hexdigest()})
            u.reg_write(regs.UC_ARM64_REG_X0, request_error if len(requests) <= fail_requests else 0)
        elif address in (BASE+0xd3bfd8, BASE+0xce440, BASE+0xce478, BASE+0xce4b0):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    try:
        u.emu_start(BASE+0xd45cb0, STOP, count=10000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'requests': requests,
            'saved_variant': struct.unpack('<I', u.mem_read(CTX+0x488, 4))[0],
            'success_byte': bytes(u.mem_read(CTX+0x48c, 1))[0]}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    default = policy(pe, 1)['remapper']
    assert default == 1
    images = {
        1: [('0x1405800', 67508, '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4'),
            ('0x13f5040', 67508, '6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4')],
        0: [('0x13e2180', 77492, 'a35d5fa8e06a86ed17d796027a6d7dea1d04b728ddd349e9d7d1e6d0e8892ccb'),
            ('0x13cf2c0', 77492, 'b36129012b36ec7a3096371b0d049b1a42e5be4eff1dcdc3f4317bb113fda647')]}
    rows = []
    for mode in (default, 0, 2):
        group = images[1 if mode == 1 else 0]
        for kwargs, expected_indices, status, saved, success in (
                ({}, [0], 0, 0, 1),
                ({'fail_requests': 1}, [0, 1], 0, 1, 1),
                ({'fail_requests': 3}, [0, 1], 0x1f, 0, 0),
                ({'fail_requests': 1, 'previous_success': True}, [0], 0x65, 0, 1),
                ({'initial_variant': 1}, [1], 0, 1, 1),
                ({'initial_variant': 2}, [], 0x1f, 2, 0),
                ({'initial_variant': 3}, [0], 0, 0, 1)):
            result = run(pe, mode, **kwargs)
            expected_requests = [{'rva': group[i][0], 'bytes': group[i][1], 'sha256': group[i][2]}
                                 for i in expected_indices]
            assert result == {'status': status, 'requests': expected_requests,
                              'saved_variant': saved, 'success_byte': success}, result
            rows.append({'remapper': mode, 'injected': kwargs, **result})
    invalid = run(pe, 3)
    assert invalid == {'status': 0x1f, 'requests': [], 'saved_variant': 0, 'success_byte': 0}
    rows.append({'remapper': 3, **invalid})
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-extract-') as tmp:
        for mode in (0, 1, 2):
            group = images[1 if mode == 1 else 0]
            for variant in (0, 1):
                output = Path(tmp)/f'{mode}-{variant}.bin'
                extracted = extract_boot_variant(path, output, mode, variant)
                assert extracted['firmware_rva'] == group[variant][0]
                assert extracted['bytes'] == output.stat().st_size == group[variant][1]
                assert hashlib.sha256(output.read_bytes()).hexdigest() == group[variant][2]
                try:
                    extract_boot_variant(path, output, mode, variant)
                except FileExistsError:
                    pass
                else:
                    raise AssertionError('Extractor overwrote an existing file')
        for mode, variant in ((3, 0), (1, 2)):
            output = Path(tmp)/'invalid.bin'
            try:
                extract_boot_variant(path, output, mode, variant)
            except ValueError:
                pass
            else:
                raise AssertionError('Unsupported variant selection accepted')
            assert not output.exists()
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'constructor_default_remapper': default, 'cases': rows,
                      'original_stack_cookie_helpers': True,
                      'verified_variant_extractions': 6, 'existing_outputs_preserved': True,
                      'limitations': ['Chip ID getter returns the independently classified P16 value 0x254',
                                      'Auxiliary transport boundary is stubbed, including injected failures',
                                      'Installed Windows registry overrides and hardware acceptance remain unknown',
                                      'Opaque variants are not interpreted or authenticated by this test']}, indent=2))


if __name__ == '__main__':
    main()
