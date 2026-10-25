#!/usr/bin/env python3
"""Execute the Windows BIT/l-table parser on synthetic offline VBIOS images.

The original parser and byte-format unpacker run unmodified. Memset/logging
and, for the physical-fallback replay, owner lookup and mappings are stubbed.
No hardware, firmware loading or physical register access occurs.
"""
import hashlib
import json
import struct
import ctypes
import errno
import os
from pathlib import Path
import random
import shlex
import subprocess
import tempfile

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
SETTINGS, OUTER, STACK, STUB, ROM = (0x3a000000+i*0x200000 for i in range(5))
STOP = STUB+0xff0


class Policy(ctypes.Structure):
    _fields_ = [('frequency_khz', ctypes.c_uint32), ('entry_frequency_khz', ctypes.c_uint32),
                ('exit_frequency_khz', ctypes.c_uint32), ('power_cap_word', ctypes.c_uint32),
                ('tuning', ctypes.c_uint32*9), ('gating_word', ctypes.c_uint32),
                ('frequency_header', ctypes.c_uint16*2), ('tuning_header', ctypes.c_uint16*2),
                ('gating_header', ctypes.c_uint16*2), ('gating_tail', ctypes.c_uint16*2),
                ('frequency_flags', ctypes.c_uint8*2), ('gating_flags', ctypes.c_uint8),
                ('gating_bytes', ctypes.c_uint8*3),
                ('feature_mask', ctypes.c_uint32), ('general_word', ctypes.c_uint32),
                ('general_header', ctypes.c_uint16*3), ('allocation_words', ctypes.c_uint16*4),
                ('boot_tables_valid', ctypes.c_uint8)]


class Location(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'bit_offset', 'rom_base', 'directory_offset', 'frequency_offset', 'tuning_offset', 'gating_offset')]


def compare_c(pe):
    with tempfile.TemporaryDirectory(prefix='n1x-vbios-policy-') as tmp:
        library = Path(tmp)/'policy.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_vbios.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    pointer = ctypes.POINTER(ctypes.c_uint8)
    lib.n1x_dla_decode_vbios_policy.argtypes = [pointer, ctypes.c_size_t]*3+[ctypes.POINTER(Policy)]
    lib.n1x_dla_parse_vbios_policy.argtypes = [pointer, ctypes.c_size_t, ctypes.POINTER(Policy),
                                              ctypes.POINTER(Location)]

    def parse(image, null=None):
        result, location = Policy(), Location()
        for obj in (result, location):
            ctypes.memset(ctypes.byref(obj), 0xa5, ctypes.sizeof(obj))
        before = bytes(result), bytes(location)
        buf = (ctypes.c_uint8*len(image)).from_buffer_copy(image)
        code = lib.n1x_dla_parse_vbios_policy(None if null == 0 else buf, len(image),
            None if null == 1 else ctypes.byref(result), None if null == 2 else ctypes.byref(location))
        if code:
            assert (bytes(result), bytes(location)) == before
        return code, result, location

    def decode(tables, null=None):
        result = Policy()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        args = []
        for i, table in enumerate(tables):
            buf = (ctypes.c_uint8*len(table)).from_buffer_copy(table)
            args += [None if null == i else buf, len(table)]
        code = lib.n1x_dla_decode_vbios_policy(*args, None if null == 3 else ctypes.byref(result))
        if code:
            assert bytes(result) == before
        return code, result

    rng = random.Random(0x200a)
    cases = 0
    for version in (0x10, 0x1f, 0x20, 0xff):
        for _ in range(8):
            image = bytearray(fixture())
            tables = []
            for offset, size in ((0x500, 18), (0x600, 32), (0x700, 16)):
                table = bytes([version])+rng.randbytes(size-1)
                image[offset:offset+size] = table
                tables.append(table)
            for offset, size in ((0x300, 14), (0x400, 8)):
                image[offset:offset+size] = bytes([version])+rng.randbytes(size-1)
            original = run(pe, bytes(image))
            assert original['status'] == 0
            fields = original['fields']
            code, result = decode(tables)
            assert code == 0
            expected = [fields[hex(i)] for i in (*range(0xa8, 0xb4, 4), *range(0xbc, 0xe4, 4))]
            assert [result.frequency_khz, result.entry_frequency_khz, result.exit_frequency_khz,
                    result.power_cap_word, *result.tuning] == expected
            assert result.gating_word == fields['0xf0']
            for name, offset in (('frequency_header', 0x98), ('tuning_header', 0xb4),
                                 ('gating_header', 0xe4), ('gating_tail', 0x100),
                                 ('frequency_flags', 0xa0), ('gating_bytes', 0xf4)):
                values = list(getattr(result, name))
                assert values == [fields[hex(offset+i*4)] for i in range(len(values))]
            assert result.gating_flags == fields['0xec']
            code, parsed, location = parse(image)
            assert code == 0 and parsed.boot_tables_valid == 1 and result.boot_tables_valid == 0
            assert parsed.feature_mask == fields['0x80']
            assert parsed.general_word == fields['0x84']
            assert list(parsed.general_header) == [fields[hex(i)] for i in range(0x74, 0x80, 4)]
            assert list(parsed.allocation_words) == [fields[hex(i)] for i in range(0x88, 0x98, 4)]
            for name in ('feature_mask', 'general_word', 'general_header', 'allocation_words', 'boot_tables_valid'):
                setattr(result, name, getattr(parsed, name))
            assert bytes(parsed) == bytes(result)
            assert [getattr(location, name) for name, _ in Location._fields_] == [0x100, 0, 0x200, 0x500, 0x600, 0x700]
            cases += 1
    failures = 0
    for i, table in enumerate(tables):
        for length in range(len(table)):
            short = list(tables)
            short[i] = table[:length]
            assert decode(short)[0] == -errno.EMSGSIZE
            failures += 1
        for version in (0, 0xf):
            invalid = list(tables)
            invalid[i] = bytes([version])+table[1:]
            assert decode(invalid)[0] == -errno.EOPNOTSUPP
            failures += 1
    for null in range(4):
        assert decode(tables, null=null)[0] == -errno.EINVAL
        failures += 1
    for entry_size in (6, 8):
        code, result, location = parse(fixture(rom_base=0x1000, bit_offset=0x1100, entry_size=entry_size))
        assert code == 0 and location.rom_base == 0x1000 and location.frequency_offset == 0x1500
        assert result.frequency_khz == 650000
    invalid_images = []
    for size in (0, 1, 11):
        invalid_images.append((bytes(size), -errno.EMSGSIZE))
    invalid_images += [(bytes(0x110001), -errno.EFBIG), (bytes(0x1000), -errno.ENOENT),
                       (fixture(token=0x70), -errno.ENOENT),
                       (fixture(corrupt_checksum=True), -errno.ENOENT)]
    for offset, fmt, value, expected in (
        (0, '<H', 0, -errno.EBADMSG),
        (0x108, '<B', 11, -errno.EBADMSG),
        (0x109, '<B', 5, -errno.EBADMSG),
        (0x10e, '<H', 35, -errno.EBADMSG),
        (0x110, '<I', 0, -errno.EBADMSG),
        (0x110, '<I', 0xffffffff, -errno.EBADMSG),
        (0x200, '<I', 0, -errno.EBADMSG),
        (0x204, '<I', 0, -errno.EBADMSG),
        (0x200, '<I', 0xfffffff0, -errno.EBADMSG),
        (0x204, '<I', 0xfffffff0, -errno.EBADMSG),
        (0x200, '<I', 0x10fff3, -errno.EBADMSG),
        (0x204, '<I', 0x10fff9, -errno.EBADMSG),
        (0x300, '<B', 0xf, -errno.EOPNOTSUPP),
        (0x400, '<B', 0xf, -errno.EOPNOTSUPP),
        (0x208, '<I', 0, -errno.EBADMSG),
        (0x20c, '<I', 0xffffffff, -errno.EBADMSG),
        (0x210, '<I', 0x10fff8, -errno.EBADMSG),
        (0x500, '<B', 0xf, -errno.EOPNOTSUPP)):
        modified = bytearray(fixture())
        struct.pack_into(fmt, modified, offset, value)
        if offset in (0x108, 0x109):
            modified[0x10b] = 0
            modified[0x10b] = (-sum(modified[0x100:0x10c])) & 255
        invalid_images.append((modified, expected))
    for size in (0x108, 0x10f, 0x220, 0x70f):
        invalid_images.append((fixture()[:size], -errno.EBADMSG if size >= 0x10c else -errno.ENOENT))
    duplicate = bytearray(fixture())
    duplicate[0x900:0x914] = duplicate[0x100:0x114]
    invalid_images.append((duplicate, -errno.EEXIST))
    duplicate = bytearray(fixture())
    duplicate[0x114:0x11c] = duplicate[0x10c:0x114]
    duplicate[0x10a] = 2
    duplicate[0x10b] = 0
    duplicate[0x10b] = (-sum(duplicate[0x100:0x10c])) & 255
    invalid_images.append((duplicate, -errno.EEXIST))
    flood = bytearray(fixture(token=0x70))
    header = bytes(flood[0x100:0x114])
    for i in range(33):
        flood[0x100+i*0x20:0x114+i*0x20] = header
    invalid_images.append((flood, -errno.E2BIG))
    for image, expected in invalid_images:
        assert parse(image)[0] == expected, (len(image), expected, parse(image)[0])
    for null in range(3):
        assert parse(fixture(), null=null)[0] == -errno.EINVAL
    return {'differential_cases': cases, 'invalid_input_cases': failures,
            'rom_parser_negative_cases': len(invalid_images)+3}


def fixture(*, rom_base=0, bit_offset=0x100, entry_size=8, token=0x6c,
            table_version=0x10, corrupt_checksum=False):
    image = bytearray(0x110000)
    struct.pack_into('<H', image, rom_base, 0xaa55)
    # BIT signature/version/header-size/entry-size/count/checksum.
    header = bytearray(struct.pack('<HIHBBBB', 0xb8ff, 0x544942, 0x100, 12, entry_size, 1, 0))
    header[-1] = (-sum(header)) & 255
    if corrupt_checksum:
        header[-1] ^= 1
    image[bit_offset:bit_offset+12] = header
    entry = struct.pack('<BBHI', token, 1, 36, 0x200) if entry_size == 8 else struct.pack('<BBHH', token, 1, 36, 0x200)
    image[bit_offset+12:bit_offset+12+len(entry)] = entry
    tables = [0x300+i*0x100 for i in range(9)]
    struct.pack_into('<9I', image, rom_base+0x200, *tables)
    # Remaining tables deliberately have version 0, so Windows skips them.
    # Supply general, allocation, frequency, tuning and gating policy tables.
    struct.pack_into('<HHHII', image, rom_base+tables[0], table_version, 14, 0x1234, 0x23456789, 0x3456789a)
    struct.pack_into('<4H', image, rom_base+tables[1], table_version, 8, 0x4567, 0x5678)
    struct.pack_into('<HHBBIII', image, rom_base+tables[2], table_version, 18, 1, 2,
                     650000, 500000, 600000)
    struct.pack_into('<HHIIHHHIIHHH', image, rom_base+tables[3], table_version, 32,
                     0x12345678, 0x10203040, 0x1122, 0x2233, 0x3344,
                     0x20304050, 0x30405060, 0x4455, 0x5566, 0x6677)
    struct.pack_into('<HHBIBBBHH', image, rom_base+tables[4], table_version, 16, 3,
                     0x40506070, 4, 5, 6, 0x7788, 0x8899)
    return bytes(image)


def run(pe, image, *, entry=0xd3d010, locator_word=0):
    assert len(image) == 0x110000
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd3b000, 0xd3c000, 0xd3d000, 0xd39000, 0xd57000, 0xcd000, 0xce000, 0xeec000,
                 0xf0e000, 0xf32000, 0xf34000, 0x108e000, 0x1512000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (SETTINGS, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    u.mem_map(ROM, len(image))
    u.mem_write(ROM, image)
    u.mem_write(SETTINGS+0x248, struct.pack('<Q', ROM))
    u.mem_write(BASE+0x1512148, struct.pack('<Q', OUTER))
    u.mem_write(STUB+0x100, struct.pack('<I', locator_word))
    install_cookie(u, pe)
    parsed = []
    mappings = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xcd148:
            assert reg(1) == 0 and reg(2) <= 0x250
            u.mem_write(reg(0), bytes(reg(2)))
            ret()
        elif address == BASE+0xce4b0:
            ret()
        elif address == BASE+0xd39160:
            assert reg(0) == OUTER
            ret(OUTER)
        elif address == BASE+0xd3b4f8:
            assert reg(0) == OUTER and reg(3) == 0
            physical, length = reg(1), reg(2)
            mappings.append({'map': hex(physical), 'length': length})
            if len(mappings) == 1:
                assert (physical, length) == (0x1182bc80, 4)
                ret(STUB+0x100)
            else:
                assert (physical, length) == (((locator_word << 12)+0x80000600) & 0xffffffff, 0x110000)
                ret(ROM)
        elif address == BASE+0xd3c048:
            assert reg(0) == 0
            assert (reg(1), reg(2)) in ((STUB+0x100, 4), (ROM, 0x110000))
            mappings.append({'unmap': 'locator' if reg(1) == STUB+0x100 else 'rom', 'length': reg(2)})
            ret()
        elif address == BASE+0xd3d5e0 and SETTINGS+0x50 <= reg(3) < SETTINGS+0x240:
            parsed.append({'rom_offset': hex(reg(2)-ROM), 'destination': hex(reg(3)-SETTINGS),
                           'format': bytes(u.mem_read(reg(1), 64)).split(b'\0', 1)[0].decode()})

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, SETTINGS)
    u.reg_write(regs.UC_ARM64_REG_X1, OUTER)
    u.reg_write(regs.UC_ARM64_REG_X2, OUTER)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    try:
        u.emu_start(BASE+entry, STOP, count=1000000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}; parsed={parsed}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    fields = {hex(offset): int.from_bytes(u.mem_read(SETTINGS+offset, 4), 'little')
              for offset in range(0x74, 0x108, 4)}
    return {'status': reg(0), 'fields': fields, 'parsed': parsed, 'mappings': mappings,
            'mapping_released': int.from_bytes(u.mem_read(SETTINGS+0x248, 8), 'little') == 0,
            'bit_offset': int.from_bytes(u.mem_read(SETTINGS+0x240, 4), 'little'),
            'rom_base': int.from_bytes(u.mem_read(SETTINGS+0x244, 4), 'little')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for entry_size in (6, 8):
        for rom_base, bit_offset in ((0, 0x100), (0x1000, 0x1100)):
            result = run(pe, fixture(rom_base=rom_base, bit_offset=bit_offset, entry_size=entry_size))
            assert result['status'] == 0 and result['rom_base'] == rom_base
            assert result['bit_offset'] == bit_offset
            fields = result['fields']
            assert [fields[hex(o)] for o in (0xa8, 0xac, 0xb0)] == [650000, 500000, 600000]
            assert [fields[hex(o)] for o in range(0xbc, 0xe4, 4)] == [
                0x12345678, 0x10203040, 0x1122, 0x2233, 0x3344,
                0x20304050, 0x30405060, 0x4455, 0x5566, 0x6677]
            assert [fields[hex(o)] for o in range(0xec, 0x108, 4)] == [
                3, 0x40506070, 4, 5, 6, 0x7788, 0x8899]
            rows.append({'entry_size': entry_size, **result})
    for version in (0, 0xf):
        result = run(pe, fixture(table_version=version))
        assert result['status'] == 0
        assert not any(result['fields'].values())
        rows.append({'unsupported_table_version': version, **result})
    for locator_word in (0, 1, 0x12345, 0x80000, 0xffffffff):
        result = run(pe, fixture(), entry=0xd3d2c0, locator_word=locator_word)
        assert result['status'] == 0 and result['mapping_released']
        assert result['fields']['0xa8'] == 650000
        assert result['mappings'] == [
            {'map': '0x1182bc80', 'length': 4},
            {'map': hex(((locator_word << 12)+0x80000600) & 0xffffffff), 'length': 0x110000},
            {'unmap': 'locator', 'length': 4}, {'unmap': 'rom', 'length': 0x110000}]
        rows.append({'synthetic_locator_word': hex(locator_word), **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'cases': rows, 'linux_decoder': compare_c(pe), 'limitations': [
                          'Synthetic VBIOS only; no actual N1X calibration values recovered',
                          'OS memset/logging and mapping/global-owner calls stubbed; parser and mapped word-read execute',
                          'Physical fallback address arithmetic tested on synthetic input, not accessed on hardware',
                          'The Windows parser can report success while skipping unsupported tables',
                          'Malformed/untrusted ROM validation must be stricter in a Linux parser']}, indent=2))


if __name__ == '__main__':
    main()
