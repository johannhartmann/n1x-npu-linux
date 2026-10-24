#!/usr/bin/env python3
"""Test Linux core control against the original Windows ARM64 observations."""
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
from emulate_core_control import run as windows

ROOT = Path(__file__).resolve().parents[1]
WRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
READ = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
DELAY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)


class IO(ctypes.Structure):
    _fields_ = [('context', ctypes.c_void_p), ('write32', WRITE), ('read32', READ), ('delay_us', DELAY)]


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-dla-core-') as tmp:
        library = Path(tmp)/'core.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_core.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    lib.n1x_dla_select_riscv.argtypes = [ctypes.POINTER(IO)]
    lib.n1x_dla_start_core.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint32]

    lib.n1x_dla_wait_idle.argtypes = lib.n1x_dla_start_core.argtypes

    def run(select=False, busy=0, high=0, bcr=0x11, fail_at=None,
            polls=5001, interval=100, no_delay=False, wait_only=False, no_write=False):
        trace, idle_values, delays = [], [], []

        @WRITE
        def write(_, offset, value):
            trace.append(('write', 0, offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @READ
        def read(_, offset, pointer):
            if offset == 0xb84:
                value = high
            elif offset == 0xe68:
                value = bcr
            elif offset == 0x4c:
                value = int(len(idle_values) < busy)
                idle_values.append(value)
            else:
                raise AssertionError('unexpected register')
            trace.append(('read', 0, offset, value))
            pointer[0] = value
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, value):
            delays.append(value)

        io = IO(None, WRITE() if no_write else write, read, DELAY() if no_delay else delay)
        code = lib.n1x_dla_select_riscv(ctypes.byref(io)) if select else (
            (lib.n1x_dla_wait_idle if wait_only else lib.n1x_dla_start_core)(ctypes.byref(io), polls, interval))
        return code, trace, idle_values, delays

    code, trace, _, _ = run(select=True)
    win_code, win_trace, _, _ = windows(pe, 0xd48c40)
    assert win_code == code == 0 and trace[1:] == win_trace
    assert trace[0] == ('read', 0, 0xb84, 0)
    for point in range(4):
        code, actual, _, _ = run(select=True, fail_at=point)
        assert code == -errno.EIO and actual == trace[:point+1]
    assert run(select=True, high=1)[0:2] == (-errno.EOPNOTSUPP, [('read', 0, 0xb84, 1)])
    for bcr in (0, 1, 0x10):
        assert run(select=True, bcr=bcr)[0] == -errno.EIO
    for busy in (0, 3, 5000):
        code, trace, idle_values, delays = run(busy=busy)
        assert code == 0 and delays == [100]*busy
        if busy < 5000:  # Windows treats the final read differently at its limit.
            win_code, win_trace, win_polls, _ = windows(pe, 0xd49e90, busy_reads=busy)
            assert win_code == 0 and trace[0:1] == win_trace and idle_values == win_polls
    code, trace, idle_values, delays = run(busy=9999)
    assert code == -errno.ETIMEDOUT and len(idle_values) == 5001 and len(delays) == 5000
    win_code, _, win_polls, _ = windows(pe, 0xd49e90, busy_reads=9999)
    assert win_code == 0x65 and idle_values == win_polls
    for point in (0, 1, 3):
        code, trace, _, _ = run(busy=5, fail_at=point)
        assert code == -errno.EIO and len(trace) == point+1
    for kwargs in ({'polls': 0}, {'polls': 5002}, {'interval': 0}, {'no_delay': True}):
        code, trace, _, _ = run(**kwargs)
        assert code == -errno.EINVAL and not trace
    assert run(polls=1, interval=0, no_delay=True)[0] == 0
    assert lib.n1x_dla_select_riscv(None) == -errno.EINVAL
    assert lib.n1x_dla_start_core(None, 1, 0) == -errno.EINVAL
    wait_cases = 0
    for busy in (0, 3, 9999):
        code, trace, idle_values, delays = run(busy=busy, wait_only=True, no_write=True)
        win_code, win_trace, win_polls, _ = windows(pe, 0xd4ce48, busy_reads=busy)
        assert win_trace == [] and all(item[0] == 'read' for item in trace)
        assert idle_values == win_polls
        assert code == (-errno.ETIMEDOUT if win_code else 0)
        assert delays == [100]*(min(busy, 5000))
        wait_cases += 1
    for point in (0, 1, 3):
        code, trace, _, _ = run(busy=5, wait_only=True, fail_at=point)
        assert code == -errno.EIO and len(trace) == point+1
    for kwargs in ({'polls': 0}, {'polls': 5002}, {'interval': 0}, {'no_delay': True}):
        code, trace, _, _ = run(wait_only=True, **kwargs)
        assert code == -errno.EINVAL and not trace
    assert run(wait_only=True, polls=1, interval=0, no_delay=True, no_write=True)[0] == 0
    assert lib.n1x_dla_wait_idle(None, 1, 0) == -errno.EINVAL
    print(json.dumps({'scope': 'Linux C core control with RAM callbacks and original Windows comparison',
                      'selection_trace_matches_windows_after_high_vector_preflight': True,
                      'start_write_and_idle_reads_match_windows': True,
                      'write_free_wait_cases': wait_cases, 'maximum_reads': 5001, 'maximum_delays': 5000,
                      'intentional_linux_differences': [
                          'Reject nonzero high vector before writes',
                          'Propagate read/write errors and invalid BCR selection',
                          'Delay only between polls; caller explicitly selects microseconds',
                          'Accept an idle value on the final permitted read'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
