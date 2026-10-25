#!/usr/bin/env python3
"""Execute the Windows BIT/l-table parser on synthetic offline VBIOS images.

The original parser and byte-format unpacker run unmodified. Only memset and
logging are stubbed. No hardware, firmware loading or register access occurs.
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
                ('gating_bytes', ctypes.c_uint8*3)]


def compare_c(pe):
    with tempfile.TemporaryDirectory(prefix='n1x-vbios-policy-') as tmp:
        library = Path(tmp)/'policy.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_vbios.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    pointer = ctypes.POINTER(ctypes.c_uint8)
    lib.n1x_dla_decode_vbios_policy.argtypes = [pointer, ctypes.c_size_t]*3+[ctypes.POINTER(Policy)]

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
    return {'differential_cases': cases, 'invalid_input_cases': failures}


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
    # Unknown/nonessential tables deliberately have version 0, so Windows skips
    # them. Supply only general, frequency, tuning and gating policy tables.
    struct.pack_into('<HHHII', image, rom_base+tables[0], table_version, 14, 0x1234, 0x23456789, 0x3456789a)
    struct.pack_into('<HHBBIII', image, rom_base+tables[2], table_version, 18, 1, 2,
                     650000, 500000, 600000)
    struct.pack_into('<HHIIHHHIIHHH', image, rom_base+tables[3], table_version, 32,
                     0x12345678, 0x10203040, 0x1122, 0x2233, 0x3344,
                     0x20304050, 0x30405060, 0x4455, 0x5566, 0x6677)
    struct.pack_into('<HHBIBBBHH', image, rom_base+tables[4], table_version, 16, 3,
                     0x40506070, 4, 5, 6, 0x7788, 0x8899)
    return bytes(image)


def run(pe, image, *, entry=0xd3d010):
    assert len(image) == 0x110000
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd3c000, 0xd3d000, 0xd57000, 0xcd000, 0xce000, 0xeec000,
                 0xf0e000, 0xf32000, 0xf34000, 0x108e000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (SETTINGS, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    u.mem_map(ROM, len(image))
    u.mem_write(ROM, image)
    u.mem_write(SETTINGS+0x248, struct.pack('<Q', ROM))
    install_cookie(u, pe)
    parsed = []

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
        elif address == BASE+0xd3d5e0 and SETTINGS+0x50 <= reg(3) < SETTINGS+0x240:
            parsed.append({'rom_offset': hex(reg(2)-ROM), 'destination': hex(reg(3)-SETTINGS),
                           'format': bytes(u.mem_read(reg(1), 64)).split(b'\0', 1)[0].decode()})

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, SETTINGS)
    u.reg_write(regs.UC_ARM64_REG_X1, OUTER)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    try:
        u.emu_start(BASE+entry, STOP, count=1000000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}; parsed={parsed}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    fields = {hex(offset): int.from_bytes(u.mem_read(SETTINGS+offset, 4), 'little')
              for offset in range(0x74, 0x108, 4)}
    return {'status': reg(0), 'fields': fields, 'parsed': parsed,
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
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'cases': rows, 'linux_decoder': compare_c(pe), 'limitations': [
                          'Synthetic VBIOS only; no actual N1X calibration values recovered',
                          'OS memset/logging stubbed; BIT scanner, table parser and unpacker execute',
                          'The Windows parser can report success while skipping unsupported tables',
                          'Malformed/untrusted ROM validation must be stricter in a Linux parser']}, indent=2))


if __name__ == '__main__':
    main()
