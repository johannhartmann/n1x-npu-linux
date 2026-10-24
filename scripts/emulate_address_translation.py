#!/usr/bin/env python3
"""Recover actual Windows resource maps and exercise the original translator.

All resources and register offsets are synthetic RAM objects. No MMIO is used.
"""
import ctypes
import errno
import os
import shlex
import subprocess
import tempfile
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
DEVICE, DLA, OUT, STACK, STOP = 0x20000000, 0x20100000, 0x20200000, 0x20300000, 0x20400000


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for addr, size in ((BASE+0xd39000, 0x1000), (BASE+0xd49000, 0x1000),
                       (BASE+0x1480000, 0x1000), (BASE+0xce000, 0x1000),
                       (DEVICE, 0x1000), (DLA, 0x1000), (OUT, 0x1000),
                       (STACK, 0x10000), (STOP, 0x1000)):
        uc.mem_map(addr, size)
    for rva in (0xd39000, 0xd49000, 0x1480000):
        uc.mem_write(BASE+rva, pe.get_data(rva, 0x1000))
    uc.mem_write(BASE+0xce4b0, bytes.fromhex('c0035fd6'))

    def call(rva, args):
        for i, value in enumerate(args):
            uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        uc.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
        uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
        uc.emu_start(BASE+rva, STOP, count=1000)
        assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return uc.reg_read(regs.UC_ARM64_REG_X0)

    with tempfile.TemporaryDirectory(prefix='n1x-dla-registers-') as tmp:
        library = Path(tmp)/'registers.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_registers.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    translate = lib.n1x_dla_register_offset
    translate.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_size_t,
                          ctypes.POINTER(ctypes.c_uint32)]
    translate.restype = ctypes.c_int

    def check_c(mode, offset, size, expected_error, expected_value):
        result = ctypes.c_uint32(0xdeadbeef)
        assert translate(mode, offset, size, ctypes.byref(result)) == expected_error
        assert result.value == expected_value

    report = []
    checks = 0
    for mode, table_rva, count_rva in ((1, 0x14803d0, 0x1480338),
                                        (2, 0x1480340, 0x1480334)):
        uc.mem_write(DEVICE, bytes(0x1000))
        for i in range(8):
            uc.mem_write(DEVICE+0x1a8+i*0x30, struct.pack('<I', i))
        uc.mem_write(DLA+0x478, struct.pack('<I', mode))
        assert call(0xd49e20, (DEVICE, DLA)) == 0
        pointer = struct.unpack('<Q', uc.mem_read(DEVICE+0x198, 8))[0]
        count = struct.unpack('<I', uc.mem_read(DEVICE+0x1a0, 4))[0]
        assert pointer == BASE+table_rva
        assert count == struct.unpack('<I', pe.get_data(count_rva, 4))[0]
        entries = [struct.unpack('<III', pe.get_data(table_rva+i*12, 12)) for i in range(count)]
        assert all(start % 4 == mapped % 4 == length % 4 == 0 for start, mapped, length in entries)
        assert all(start+length <= 0x100000000 for start, _, length in entries)
        offsets = set(range(0, 0x3104, 4)) | {0x80400330, 0xffffffff, 1}
        for start, _, length in entries:
            offsets.update((start, start+length-4, start+length, start+1))
        for offset in sorted(offsets):
            uc.mem_write(OUT, struct.pack('<I', 0xdeadbeef))
            status = call(0xd392c0, (DEVICE+0x180, offset, OUT))
            result = struct.unpack('<I', uc.mem_read(OUT, 4))[0]
            match = next(((start, mapped, length) for start, mapped, length in entries
                          if start <= offset < start+length), None)
            if offset & 3:
                assert status == 0x37 and result == 0xdeadbeef
            elif match:
                assert status == 0 and result == match[1]+offset-match[0]
            else:
                assert status == 0x1f and result == 0xdeadbeef
            check_c(mode, offset, 0x40000, 0 if status == 0 else -errno.EINVAL,
                    result)
            if status == 0:
                check_c(mode, offset, result+3, -errno.ERANGE, 0xdeadbeef)
                check_c(mode, offset, result+4, 0, result)
            checks += 1
        report.append({'mode_field_0x478': mode, 'table_rva': hex(table_rva),
                       'entries': [{'logical_start': hex(a), 'mapped_start': hex(b),
                                    'length': hex(c)} for a, b, c in entries]})
    # The original helper passes offsets through if no table is attached.
    uc.mem_write(DEVICE+0x198, bytes(8))
    for offset in (0, 0xb88, 0x80400330, 1):
        assert call(0xd392c0, (DEVICE+0x180, offset, OUT)) == 0
        assert struct.unpack('<I', uc.mem_read(OUT, 4))[0] == offset
        checks += 1
    for offset in (0, 0xb88, 0x80400330, 1):
        error = -errno.EINVAL if offset & 3 else (-errno.ERANGE if offset >= 0x40000 else 0)
        check_c(0, offset, 0x40000, error, 0xdeadbeef if error else offset)
    check_c(3, 0, 0x40000, -errno.EINVAL, 0xdeadbeef)
    assert translate(0, 0, 4, None) == -errno.EINVAL
    print(json.dumps({'scope': 'Original installer, setter and translator in RAM only',
                      'installer_rva': '0xd49e20', 'setter_rva': '0xd39ae8',
                      'translator_rva': '0xd392c0', 'verified_cases': checks,
                      'c_translation_matches_original_mapped_modes': True,
                      'c_extra_checks': 'alignment, complete u32 resource bounds, null output, unknown mode',
                      'maps': report, 'limitations': [
                          'Live mode selection and physical resource mapping remain unverified',
                          'Logical 0x80400330 is rejected by both mapped modes',
                          'Pass-through mode is not permission to access arbitrary offsets']}, indent=2))


if __name__ == '__main__':
    main()
