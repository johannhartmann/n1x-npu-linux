#!/usr/bin/env python3
"""Differential test: Linux C uploader versus original Windows ARM64 routine."""
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
from emulate_bitbang_transfer import run as run_windows
from decode_transfer_registers import EXPECTED

ROOT = Path(__file__).resolve().parents[1]
WRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)


class IO(ctypes.Structure):
    _fields_ = [('context', ctypes.c_void_p), ('write32', WRITE)]


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == '8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75'
    status, original, tail = run_windows(pe, blob)
    assert status == 0 and tail
    # The independent thunk/accessor decoder verifies this mapping from bytes.
    expected = [(EXPECTED[slot][1], value) for slot, value in original]
    assert all(EXPECTED[slot][0] == 'write' for slot, _ in original)
    with tempfile.TemporaryDirectory(prefix='n1x-dla-test-') as tmp:
        library = Path(tmp)/'transfer.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_firmware.c'),
            str(ROOT/'driver/n1x_dla_transfer.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        upload = lib.n1x_dla_upload_words
        upload.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                           ctypes.c_uint32, ctypes.POINTER(IO)]
        upload.restype = ctypes.c_int

        def run(data=blob, code_limit=0xaf00, data_limit=0xa400, fail_at=None):
            trace = []

            @WRITE
            def write(_, offset, value):
                trace.append((offset, value))
                return -errno.EIO if len(trace)-1 == fail_at else 0

            io = IO(None, write)
            buffer = ctypes.create_string_buffer(data)
            code = upload(buffer, len(data), code_limit, data_limit, ctypes.byref(io))
            return code, trace

        code, trace = run()
        assert code == 0 and trace == expected
        # Fail at entry, section boundaries, middle, final word and ITFEN.
        failures = (0, 1, 2, 3, 5000, 11377, 11378, 12000, len(expected)-2, len(expected)-1)
        for position in failures:
            code, trace = run(fail_at=position)
            assert code == -errno.EIO and trace == expected[:position+1]
        for kwargs, error in (({'data': blob[:-1]}, errno.EINVAL),
                              ({'data': bytes(len(blob))}, errno.EINVAL),
                              ({'code_limit': 0xaeff}, errno.EFBIG),
                              ({'data_limit': 0xa3ff}, errno.EFBIG)):
            code, trace = run(**kwargs)
            assert code == -error and not trace
        assert upload(None, 0, 0, 0, None) == -errno.EINVAL
    print(json.dumps({'scope': 'RAM-only C uploader versus original ARM64 Windows routine',
                      'matching_register_writes': len(expected),
                      'injected_io_failure_positions': list(failures),
                      'preflight_failures_without_writes': 5,
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
