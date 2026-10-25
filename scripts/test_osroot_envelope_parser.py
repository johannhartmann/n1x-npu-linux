#!/usr/bin/env python3
"""Replay the reference OSROOT envelope parser up to its missing key context.

No firmware authentication is simulated as successful and no hardware is used.
"""
import hashlib
import json
import struct

from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs

from test_osroot_reply import ROOT, CODE, DATA, CODE_HASH, DATA_HASH

IMAGE_HASHES = (
    '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4',
    '6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4',
)
STACK, IMAGE, CONFIG, STOP = 0x400000, 0x410000, 0x430000, 0x440000
XOR_MASK = 0xa5c35a3c


def run_parser(code, data, image, capacity=None, scheme=0x3033):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((CODE, 0x88000), (DATA, 0x3a000),
                          (STACK, 0x10000), (IMAGE, 0x20000),
                          (CONFIG, 0x1000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(CODE, code)
    u.mem_write(DATA, data)
    u.mem_write(IMAGE, image)
    capacity = len(image) if capacity is None else capacity
    # Synthetic local staging buffer: this deliberately omits the DMA path.
    # All four accepted scheme selectors are tested; their platform selection
    # has not been recovered for P16 SROOT.
    u.mem_write(CONFIG, struct.pack('<11I', scheme, 0, 0, IMAGE, capacity,
                                    0, 0, 0, 0, 0, 0))
    lookups, logs = [], []

    def instruction(_, address, size, user):
        offset = address-CODE
        if offset == 0x5004:
            # The only stub is logging; all parser/allocator/error code runs.
            fmt = u.reg_read(regs.UC_RISCV_REG_A0)
            logs.append({'format_address': hex(fmt),
                         'argument2': hex(u.reg_read(regs.UC_RISCV_REG_A2))})
            u.reg_write(regs.UC_RISCV_REG_PC, u.reg_read(regs.UC_RISCV_REG_RA))
        elif offset in (0x6e9e, 0x5fb8):
            raise AssertionError('Fatal integrity check or stack-cookie failure')
        elif offset == 0xf6ee:
            context = u.reg_read(regs.UC_RISCV_REG_A0)
            entry = u.reg_read(regs.UC_RISCV_REG_A3)
            key_id = u.reg_read(regs.UC_RISCV_REG_S2)
            assert context == 0x16e000
            assert entry == context+0x5c4+52*key_id
            record = bytes(u.mem_read(entry, 52))
            assert record == bytes(52)  # Missing runtime context, not a device read.
            lookups.append({'key_id': key_id, 'entry_address': hex(entry),
                            'digest_algorithm_word': struct.unpack_from('<I', record)[0]})

    u.hook_add(UC_HOOK_CODE, instruction)

    def invoke(offset, *arguments):
        u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        for index, value in enumerate(arguments):
            u.reg_write(getattr(regs, f'UC_RISCV_REG_A{index}'), value)
        u.emu_start(CODE+offset, STOP, count=500000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        return u.reg_read(regs.UC_RISCV_REG_A0), u.reg_read(regs.UC_RISCV_REG_A1)

    assert invoke(0xf0ec, CONFIG)[0] == 0x55aa0000
    # The original caller at 0x6088 acquires this encoded counter before parsing.
    assert invoke(0xde9e, 1) == (0, XOR_MASK)
    result, complement = invoke(0xf1d2, IMAGE, CONFIG+0x100)
    assert complement == result ^ XOR_MASK
    assert bytes(u.mem_read(0x150528, 8)) == struct.pack('<II', 0, XOR_MASK)
    after = bytes(u.mem_read(IMAGE, len(image)))
    assert after == image or after == bytes(capacity)+image[capacity:]
    staging_state = ('unchanged' if after == image else
                     'zeroed' if after == bytes(len(image)) else 'partially_changed')
    assert bytes(u.mem_read(0x16e000, 0x5c4+24*52)) == bytes(0x5c4+24*52)
    return {'return': hex(result), 'key_lookups': lookups, 'logs': logs,
            'scheme': hex(scheme), 'capacity': capacity, 'staging_state': staging_state}


def main():
    directory = ROOT/'sources/dgx-spark-osroot'
    code = (directory/'osroot-2100.bin').read_bytes()
    data = (directory/'osroot-2200.bin').read_bytes()
    assert hashlib.sha256(code).hexdigest() == CODE_HASH
    assert hashlib.sha256(data).hexdigest() == DATA_HASH
    cases = []
    for variant, expected_hash in enumerate(IMAGE_HASHES):
        source = (ROOT/f'sources/dla-p16-sroot-v{variant}.bin').read_bytes()
        assert hashlib.sha256(source).hexdigest() == expected_hash
        assert struct.unpack_from('<I', source)[0] == 0xc0302
        # Tag is sent separately; Linux's staged allocation has four zero tail
        # bytes after the Windows-sized copy. No physical DMA is modeled here.
        image = source[4:]+bytes(4)
        assert image[:3] == bytes.fromhex('59018d')
        for scheme in (0x300, 0xc11, 0x3033, 0xc077):
            result = run_parser(code, data, image, scheme=scheme)
            assert result['return'] == ('0x55aa0000' if scheme == 0x300 else '0xaa559430')
            assert result['staging_state'] == ('unchanged' if scheme == 0x300 else 'zeroed')
            assert result['logs'] == [{'format_address': ('0x146400' if scheme == 0x300
                                                          else '0x146434'),
                                      'argument2': '0xaa559430'}]
            assert result['key_lookups'] == [
                {'key_id': 8, 'entry_address': '0x16e764', 'digest_algorithm_word': 0}]
            cases.append({'variant': variant, 'control': 'original', **result})
        controls = (
            ('wrong_prefix_type', b'\0'+image[1:], None, 0xaa559411),
            ('misaligned_envelope', image[:2]+b'\x8c'+image[3:], None, 0xaa559410),
            ('insufficient_staging_capacity', image, 399, 0xaa559407),
            ('nonzero_padding', image[:399]+b'\x01'+image[400:], None, 0xaa55940c),
        )
        for name, modified, capacity, expected in controls:
            result = run_parser(code, data, modified, capacity)
            assert result['return'] == hex(expected) and result['key_lookups'] == []
            assert result['staging_state'] == ('partially_changed' if capacity else 'zeroed')
            cases.append({'variant': variant, 'control': name, **result})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'original_parser_offset': '0xf1d2', 'cases': cases,
                      'limitations': [
                          'OSROOT reference, not identified P16 SROOT firmware',
                          'Local buffer omits physical DMA and cache behavior',
                          'Runtime key context is absent and remains zero-filled',
                          'Only logging is stubbed; no successful crypto return is injected',
                          'Scheme 0x300 explicitly ignores this auth error; its return is not authentication proof',
                          'Envelope traversal reaches a key lookup, not completed authentication',
                          'Emulated 0xaa559430 is not the observed P16 status 1023/detail 37'
                      ]}, indent=2))


if __name__ == '__main__':
    main()
