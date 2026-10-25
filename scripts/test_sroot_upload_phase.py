#!/usr/bin/env python3
"""Check the connected C initial-upload phase and every I/O failure boundary."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import pefile
from test_core_control import IO, READ, WRITE, DELAY
from test_sroot_receive import Reply
from test_sroot_exchange import Bus, windows
from test_sroot_send import ROOT, HASH


class Result(ctypes.Structure):
    _fields_ = [('reply', Reply), ('upload_attempted', ctypes.c_uint8),
                ('reply_valid', ctypes.c_uint8), ('idle_observed', ctypes.c_uint8)]


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-upload-phase-') as tmp:
        library = Path(tmp)/'boot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_sroot.c'),
            str(ROOT/'driver/n1x_dla_core.c'), str(ROOT/'driver/n1x_dla_boot.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    upload = lib.n1x_dla_sroot_upload
    upload.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(IO), ctypes.c_uint32,
                       ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                       ctypes.c_uint32, ctypes.POINTER(Result)]
    words = [0xc0302, 67508, 0xe0010000]

    def linux(bus, busy=0, fail_at=None, polls=4, invalid=None):
        trace = []
        idle_reads = 0

        @READ
        def root_read(_, offset, out):
            out[0] = bus.read(offset)
            trace.append(('sroot-read', offset, out[0]))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @WRITE
        def root_write(_, offset, value):
            bus.write(offset, value)
            trace.append(('sroot-write', offset, value))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @READ
        def dla_read(_, offset, out):
            nonlocal idle_reads
            assert offset == 0x4c
            out[0] = int(idle_reads < busy)
            idle_reads += 1
            trace.append(('dla-read', offset, out[0]))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            trace.append(('delay', interval))

        # No DLA write callback exists in this test: using start_core fails.
        root_io = IO(None, root_write, root_read, delay)
        dla_io = IO(None, WRITE(), dla_read, delay)
        result = Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        data = (ctypes.c_uint32*3)(*words)
        stride, interval = bus.stride, 1
        root_arg, dla_arg, data_arg, result_arg = (
            ctypes.byref(root_io), ctypes.byref(dla_io), data, ctypes.byref(result))
        if invalid == 'sroot': root_arg = None
        if invalid == 'dla': dla_arg = None
        if invalid == 'words': data_arg = None
        if invalid == 'result': result_arg = None
        if invalid == 'stride': stride = 3
        if invalid == 'overflow_stride': stride = 0xfffffffc
        if invalid == 'zero_polls': polls = 0
        if invalid == 'large_polls': polls = 5002
        if invalid == 'interval': interval = 0
        if invalid == 'root_read': root_io.read32 = READ()
        if invalid == 'root_write': root_io.write32 = WRITE()
        if invalid == 'dla_read': dla_io.read32 = READ()
        if invalid == 'root_delay': root_io.delay_us = DELAY()
        if invalid == 'dla_delay': dla_io.delay_us = DELAY()
        ret = upload(root_arg, dla_arg, stride, data_arg, polls, interval, result_arg)
        if invalid:
            assert ret == -errno.EINVAL and not trace and not bus.trace, (invalid, ret, trace)
            assert bytes(result) == before
        return ret, result, trace

    successes = 0
    for stride in (4, 16):
        for command in (0x18, 0x77):
            for status, detail in ((0, 0), (1, 0), (0, 0x80)):
                for stale in (0, 2):
                    header = command | status << 12 | detail << 24
                    original = Bus(stride, header, [], stale=stale)
                    original_status, decoded, _ = windows(pe, original, 0x18, words, 0)
                    assert original_status == 0 and decoded == status | detail << 12
                    candidate = Bus(stride, header, [], stale=stale)
                    ret, result, trace = linux(candidate, busy=2)
                    assert candidate.trace == original.trace
                    assert result.upload_attempted == result.reply_valid == 1
                    assert result.reply.header == header and result.reply.command == command
                    assert ret == (-errno.EREMOTEIO if decoded else 0)
                    assert result.idle_observed == (not decoded)
                    assert len([e for e in trace if e[0] == 'dla-read']) == (0 if decoded else 3)
                    successes += 1
    baseline = Bus(16, 0x18, [])
    _, _, trace = linux(baseline)
    for index in range(len(trace)):
        ret, result, stopped_trace = linux(Bus(16, 0x18, []), fail_at=index)
        assert ret == -errno.EIO and stopped_trace == trace[:index+1]
        assert result.upload_attempted == 1 and result.idle_observed == 0
        assert result.reply_valid == (trace[index][0] == 'dla-read')
    for suppress in (2, 5, 8):
        bus = Bus(16, 0x18, [])
        bus.suppress = suppress
        ret, result, _ = linux(bus)
        assert ret == -errno.ETIMEDOUT
        assert result.upload_attempted and not result.reply_valid and not result.idle_observed
    ret, result, _ = linux(Bus(16, 0x18, []), busy=4)
    assert ret == -errno.ETIMEDOUT and result.reply_valid and not result.idle_observed
    ret, result, _ = linux(Bus(16, 0x18, [], busy=1))
    assert ret == -errno.EBUSY and result.upload_attempted and not result.reply_valid
    invalids = ('sroot', 'dla', 'words', 'result', 'stride', 'overflow_stride',
                'zero_polls', 'large_polls', 'interval', 'root_read', 'root_write',
                'dla_read', 'root_delay', 'dla_delay')
    for invalid in invalids:
        linux(Bus(16, 0x18, []), invalid=invalid)
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_matching_transport_cases': successes,
                      'injected_io_failure_positions': len(trace),
                      'preflight_rejections': len(invalids),
                      'limitations': ['SROOT readiness/replies and DLA idle are synthetic',
                                      'DMA storage is caller-owned; no allocation, physical publication or free occurs here',
                                      'The upload-attempt flag conservatively retains storage even on preparation failure',
                                      'No IRQ setup, ping, full initialization, physical boot or inference']}, indent=2))


if __name__ == '__main__':
    main()
