#!/usr/bin/env python3
"""Revalidate HAL-1 accessors, firmware word upload and shared core callbacks."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import pefile
from extract_firmware import KMD_SHA256
from extract_profile1_firmware import PROFILE1_HASH
from emulate_firmware_commands import ROOT
from emulate_register_accessors import decode
from emulate_bitbang_transfer import run as windows_upload
from emulate_core_control import run as windows_core
from decode_transfer_registers import EXPECTED
from test_word_upload import IO, WRITE


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    accessors = decode(pe, 1)
    assert accessors == decode(pe, 2) and len(accessors) == 78
    # Execute both original HAL initializers and inspect the callback pointers,
    # rather than trusting a previously generated JSON inventory.
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from unicorn import arm64_const as regs
    base, context, stop = 0x140000000, 0x21000000, 0x21100000
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(base+0x1951000, 0xd000)
    u.mem_write(base+0x1951000, pe.get_data(0x1951000, 0xd000))
    u.mem_map(context, 0x1000)
    u.mem_map(stop, 0x1000)
    import struct
    callbacks = []
    for selector in (1, 2):
        u.mem_write(context, bytes(0x1000))
        u.mem_write(context+0x496, struct.pack('<H', selector))
        u.reg_write(regs.UC_ARM64_REG_X0, context)
        u.reg_write(regs.UC_ARM64_REG_LR, stop)
        u.emu_start(base+0x1951238, stop, count=50000)
        assert u.reg_read(regs.UC_ARM64_REG_PC) == stop
        callbacks.append(struct.unpack('<Q', u.mem_read(context+0x3e0, 8))[0])
    assert callbacks == [base+0xd48c40]*2
    code, trace, _, _ = windows_core(pe, 0xd48c40)
    assert code == 0 and trace == [('write', 0, 0xb80, 0x100000),
                                  ('write', 0, 0xe68, 0x10), ('read', 0, 0xe68, 0x11)]
    status, writes, continuation = windows_upload(pe, blob)
    assert status == 0 and continuation and len(writes) == 16839
    expected = [(EXPECTED[slot][1], value) for slot, value in writes]
    with tempfile.TemporaryDirectory(prefix='n1x-profile1-upload-') as tmp:
        library = Path(tmp)/'transfer.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_firmware.c'), str(ROOT/'driver/n1x_dla_transfer.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    upload = lib.n1x_dla_upload_words
    upload.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(IO)]

    def linux(code_limit=36864, data_limit=29440, fail_at=None):
        trace = []

        @WRITE
        def write(_, offset, value):
            trace.append((offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        io = IO(None, write, None, None)
        status = upload(blob, len(blob), code_limit, data_limit, ctypes.byref(io))
        return status, trace

    assert linux() == (0, expected)
    failures = (0, 2, 5000, 9360, 10000, 16838)
    for position in failures:
        assert linux(fail_at=position) == (-errno.EIO, expected[:position+1])
    for code_limit, data_limit in ((36863, 29440), (36864, 29439)):
        assert linux(code_limit, data_limit) == (-errno.EFBIG, [])
    print(json.dumps({'passed': True, 'hardware_access': False, 'firmware_sha256': PROFILE1_HASH,
                      'identical_accessor_cases': 78, 'matching_upload_writes': 16839,
                      'io_failure_positions': failures, 'preflight_size_failures': 2,
                      'shared_select_callback_rva': '0xd48c40',
                      'limitations': ['Readback verification and physical register backend are stubbed',
                                      'Power/reset, remapper selection and signed boot variants remain unresolved',
                                      'This does not establish firmware boot or protocol readiness on hardware']}, indent=2))


if __name__ == '__main__':
    main()
