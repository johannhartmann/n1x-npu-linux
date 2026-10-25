#!/usr/bin/env python3
"""Check C version/power commands through rings against original ARM64 policy.

No device access. The peer supplies synthetic firmware responses; the Windows
functions execute in Unicorn with their transport stubbed by existing tests.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

import pefile
from test_hfrp_channel import ROOT, IO, Channel, Result, Peer, NEW, READY, ACTIVE, FAILED
from test_hfrp_version import run as windows_version
from test_hfrp_power_policy import run as windows_power
from extract_firmware import KMD_SHA256


class Version(ctypes.Structure):
    _fields_ = [('stamp', ctypes.c_uint32), ('response_word', ctypes.c_uint32),
                ('stamp_valid', ctypes.c_uint8), ('query_requested', ctypes.c_uint8),
                ('valid', ctypes.c_uint8), ('major', ctypes.c_uint8), ('minor', ctypes.c_uint8)]


class StartupPeer(Peer):
    def __init__(self, stamp=0x20260101, response=0x00010500):
        super().__init__()
        self.put(0, stamp)
        self.response = struct.pack('<I', response)

    def enqueue(self, sequence, status=0, payload=None):
        command = (int.from_bytes(self.packets[-1][:4], 'little') >> 18) & 1023
        super().enqueue(sequence, status, self.response if command == 999 else b'')


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-startup-') as tmp:
        library = Path(tmp)/'startup.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_hfrp.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    ptr = ctypes.POINTER
    lib.n1x_hfrp_handshake.argtypes = [ptr(IO), ptr(Channel), ctypes.c_uint32, ctypes.c_uint32, ptr(Result)]
    lib.n1x_hfrp_discover_version.argtypes = [ptr(IO), ptr(Channel), ctypes.c_uint32, ctypes.c_uint32,
                                             ptr(Version), ptr(Result)]
    lib.n1x_hfrp_power_on.argtypes = [ptr(IO), ptr(Channel), ptr(Version), ctypes.c_uint8,
                                     ctypes.c_uint32, ctypes.c_uint32, ptr(Result)]
    rows = []

    def start(stamp=0x20260101, response=0x00010500):
        peer, channel, result = StartupPeer(stamp, response), Channel(), Result()
        assert lib.n1x_hfrp_handshake(ctypes.byref(peer.io), ctypes.byref(channel), 4, 10,
                                      ctypes.byref(result)) == 0
        assert result.mode_selected and not peer.errors
        return peer, channel

    def discover(peer, channel, version=None):
        version = version if version is not None else Version()
        result = Result()
        code = lib.n1x_hfrp_discover_version(ctypes.byref(peer.io), ctypes.byref(channel),
                                             4, 10, ctypes.byref(version), ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, version, result

    def power(peer, channel, version, pps):
        result = Result()
        code = lib.n1x_hfrp_power_on(ctypes.byref(peer.io), ctypes.byref(channel), ctypes.byref(version),
                                    pps, 4, 10, ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, result

    for stamp in (0, 0x20250210, 0x20250211, 0x20250424, 0x20250425, 0x2025fffe,
                  0x2025ffff, 0x20260101, 0xffffffff):
        for response in (0x00010300, 0x000105ab, 0x00010600, 0x99020733):
            peer, channel = start(stamp, response)
            code, version, result = discover(peer, channel)
            original = windows_version(pe, stamp, response=response)
            assert version.stamp_valid and version.stamp == stamp
            assert version.query_requested == (stamp >= 0x2025ffff)
            assert code == (-errno.EOPNOTSUPP if stamp < 0x20250211 else 0)
            assert bool(version.valid) == (code == 0) and channel.state == READY
            assert len(peer.packets) == (2 if version.query_requested else 1)
            if version.valid:
                assert [version.major, version.minor] == original['version']
                if version.query_requested:
                    assert version.response_word == response and result.reply_valid
                for pps in (0, 1, 3, 255):
                    before = len(peer.trace)
                    power_code, power_result = power(peer, channel, version, pps)
                    original_power = windows_power(pe, version=(version.major, version.minor), pps=(0, pps, 0, 0))
                    commands = [e for e in original_power['events'] if 'command' in e]
                    if version.major != 1:
                        assert power_code == -errno.EOPNOTSUPP and not commands
                        assert len(peer.trace) == before
                    else:
                        packet = peer.packets[-1]
                        assert power_code == 0 and power_result.reply_valid
                        assert (int.from_bytes(packet[:4], 'little') >> 18) & 1023 == 400
                        assert commands == [{'command': '0x190', 'payload': packet[4:].hex()}]
            rows.append({'stamp': hex(stamp), 'response': hex(response), 'status': code,
                         'version': [version.major, version.minor], 'valid': bool(version.valid)})

    # Never use an old successful discovery after an accepted but malformed query.
    failures = []
    for length in (0, 1, 3, 5, 50):
        peer, channel = start()
        _, version, _ = discover(peer, channel)
        peer.response = bytes(length)
        code, version, result = discover(peer, channel, version)
        assert code == -errno.EBADMSG and not version.valid and channel.state == FAILED
        assert result.reply_valid and result.reply.length == length
        before = len(peer.trace)
        assert power(peer, channel, version, 1)[0] == -errno.EINVAL
        assert len(peer.trace) == before
        failures.append({'query_length': length, 'status': code})
    for failure in ('stamp_read', 'firmware', 'timeout'):
        peer, channel = start()
        _, version, _ = discover(peer, channel)
        if failure == 'stamp_read': peer.fail_at = len(peer.trace)
        if failure == 'firmware': peer.status = 0x123
        if failure == 'timeout': peer.never_reply = True
        code, version, _ = discover(peer, channel, version)
        expected = {'stamp_read': -errno.EIO, 'firmware': -errno.EREMOTEIO, 'timeout': -errno.ETIMEDOUT}[failure]
        assert code == expected and not version.valid
        assert channel.state == (READY if failure == 'firmware' else FAILED)
        failures.append({'failure': failure, 'status': code})
    for state in (NEW, ACTIVE, FAILED):
        peer, channel, version = StartupPeer(), Channel(), Version()
        channel.state = state
        ctypes.memset(ctypes.byref(version), 0xa5, ctypes.sizeof(version))
        before = bytes(version)
        code, version, _ = discover(peer, channel, version)
        assert code == (-errno.ENOTRECOVERABLE if state == FAILED else -errno.EBUSY)
        assert bytes(version) == before and not peer.trace
    for major, minor in ((1, 2), (1, 4), (2, 3)):
        peer, channel = start()
        version = Version(valid=1, major=major, minor=minor)
        before = len(peer.trace)
        assert power(peer, channel, version, 1)[0] == -errno.EOPNOTSUPP
        assert len(peer.trace) == before
    for failure, expected in (('firmware', -errno.EREMOTEIO), ('timeout', -errno.ETIMEDOUT)):
        peer, channel = start()
        _, version, _ = discover(peer, channel)
        peer.status = 1 if failure == 'firmware' else 0
        peer.never_reply = failure == 'timeout'
        assert power(peer, channel, version, 1)[0] == expected
        assert channel.state == (READY if failure == 'firmware' else FAILED)
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'version_and_power_cases': rows, 'discovery_failures': failures,
                      'limitations': ['Synthetic firmware replies; Windows transport stubbed',
                                      'Does not establish physical power state or startup configuration',
                                      'PPS values are test inputs, not calibrated hardware settings']}, indent=2))


if __name__ == '__main__':
    main()
