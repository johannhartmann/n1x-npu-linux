#!/usr/bin/env python3
"""Compare Linux startup exchanges with Windows packets and inject peer faults."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import struct
import subprocess
import tempfile

import pefile
from extract_firmware import KMD_SHA256
from test_hfrp_channel import ROOT, IO, Channel, Result, Peer, READY, FAILED
from test_hfrp_startup import Version
from test_hfrp_vbios import Policy
from test_hfrp_config import run as windows, CURVE


class SetupResult(ctypes.Structure):
    _fields_ = [('exchange', Result), ('frequency_khz', ctypes.c_uint32*8),
                ('voltage_word', ctypes.c_uint16*8), ('command', ctypes.c_uint16),
                ('completed_commands', ctypes.c_uint8), ('curve_count', ctypes.c_uint8),
                ('configured', ctypes.c_uint8)]


class SetupPeer(Peer):
    def __init__(self, failure=None, at=None, curve=CURVE, padded=False):
        super().__init__()
        self.failure, self.at, self.curve, self.padded = failure, at, curve, padded
        self.setup_commands = 0

    def enqueue(self, sequence, status=0, payload=None):
        packet = self.packets[-1]
        command = (int.from_bytes(packet[:4], 'little') >> 18) & 1023
        if command == 1:
            return super().enqueue(sequence, status, b'')
        index = self.setup_commands
        self.setup_commands += 1
        if command == 428:
            payload = struct.pack('<H', len(self.curve))+b''.join(struct.pack('<HH', *pair) for pair in self.curve)
            if self.padded: payload = payload.ljust(34, b'\0')
        elif command == 432:
            payload = bytes(26)
        else:
            payload = bytes(len(packet)-4)
        if index == self.at:
            if self.failure == 'timeout': return
            if self.failure == 'firmware': status = 7
            if self.failure == 'short': payload = payload[:-1]
            if self.failure == 'long': payload += b'\0'
            if self.failure == 'vf_zero': payload = bytes(34)
            if self.failure == 'vf_count9': payload = b'\x09\0'+bytes(32)
            if self.failure == 'vf_truncated': payload = b'\x08\0'+bytes(28)
        super().enqueue(sequence, status, payload)


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-setup-') as tmp:
        library = Path(tmp)/'setup.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            *[str(ROOT/'driver'/name) for name in ('n1x_hfrp.c', 'n1x_dla_vbios.c', 'n1x_hfrp_setup.c')],
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    ptr = ctypes.POINTER
    lib.n1x_hfrp_handshake.argtypes = [ptr(IO), ptr(Channel), ctypes.c_uint32, ctypes.c_uint32, ptr(Result)]
    lib.n1x_hfrp_configure_policy.argtypes = [ptr(IO), ptr(Channel), ptr(Version), ptr(Policy),
                                            ctypes.c_uint32, ctypes.c_uint32, ptr(SetupResult)]
    lib.n1x_hfrp_configure_policy.restype = ctypes.c_int
    rng = random.Random(0x200a)

    def start(**kwargs):
        peer, channel, result = SetupPeer(**kwargs), Channel(), Result()
        assert lib.n1x_hfrp_handshake(ctypes.byref(peer.io), ctypes.byref(channel), 4, 10, ctypes.byref(result)) == 0
        assert not peer.errors and channel.state == READY
        return peer, channel

    def call(peer, channel, policy, version=None, result=None):
        version = version if version is not None else Version(valid=1, major=1, minor=5)
        result = result if result is not None else SetupResult()
        code = lib.n1x_hfrp_configure_policy(ctypes.byref(peer.io), ctypes.byref(channel),
            ctypes.byref(version), ctypes.byref(policy), 4, 10, ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, result

    packet_cases = 0
    for minor in (3, 5, 6):
        for padded in (False, True):
            for pps in ((0, 0), (1, 3), (255, 255)):
                policy = Policy(boot_tables_valid=1, frequency_khz=650000,
                                entry_frequency_khz=rng.getrandbits(32), exit_frequency_khz=rng.getrandbits(32),
                                power_cap_word=rng.getrandbits(32))
                policy.tuning[:] = [rng.getrandbits(32) for _ in range(9)]
                policy.gating_bytes[:] = [0, *pps]
                settings = {0xac: policy.entry_frequency_khz, 0xb0: policy.exit_frequency_khz, 0xbc: policy.power_cap_word}
                settings.update({0xc0+4*i: value for i, value in enumerate(policy.tuning)})
                original = windows(pe, version=(1, minor), settings=settings,
                                   power_states=(pps[0] or 1, pps[1] or 3, 15))
                expected = [(x['command'], x['payload']) for x in original['events'] if 'command' in x]
                peer, channel = start(padded=padded)
                before = bytes(policy)
                code, result = call(peer, channel, policy, Version(valid=1, major=1, minor=minor))
                assert code == 0 and result.configured and channel.state == READY
                assert bytes(policy) == before
                assert result.completed_commands == (5 if minor == 3 else 8)
                actual = [((int.from_bytes(p[:4], 'little') >> 18) & 1023, p[4:].hex()) for p in peer.packets[1:]]
                assert actual == expected, (minor, actual, expected)
                assert result.curve_count == 3
                assert list(result.frequency_khz) == [400000, 900000, 600000]+[0]*5
                assert list(result.voltage_word) == [700, 850, 800]+[0]*5
                assert all(command != 400 for command, _ in actual)
                packet_cases += 1

    failures = 0
    for index, command in enumerate((412, 401, 428, 408, 407, 432, 432, 432)):
        for failure, expected in (('firmware', -errno.EREMOTEIO), ('timeout', -errno.ETIMEDOUT),
                                  ('short', -errno.EBADMSG), ('long', -errno.EBADMSG)):
            peer, channel = start(failure=failure, at=index)
            code, result = call(peer, channel, policy)
            assert code == expected and channel.state == FAILED, (index, failure, code)
            assert not result.configured and result.completed_commands == index and result.command == command
            assert peer.setup_commands == index+1
            # A second call must not resend partially applied startup commands.
            before = len(peer.trace), bytes(result), bytes(channel)
            assert call(peer, channel, policy, result=result)[0] == -errno.ENOTRECOVERABLE
            assert (len(peer.trace), bytes(result), bytes(channel)) == before
            failures += 1
    for failure in ('vf_zero', 'vf_count9', 'vf_truncated'):
        peer, channel = start(failure=failure, at=2)
        code, result = call(peer, channel, policy)
        assert code == -errno.EBADMSG and channel.state == FAILED
        assert result.completed_commands == 2 and peer.setup_commands == 3
        failures += 1
    for curve in (((1, 1),), tuple((65535, 65535) for _ in range(8))):
        peer, channel = start(curve=curve)
        code, result = call(peer, channel, policy)
        assert code == 0 and result.curve_count == len(curve)
        assert list(result.frequency_khz)[:len(curve)] == [f*1000 for _, f in curve]
    peer, channel = start()
    channel.next_sequence = 1021
    assert call(peer, channel, policy)[0] == 0
    assert [(int.from_bytes(p[:4], 'little') >> 8) & 1023 for p in peer.packets[1:]] == [1021, 1022, 0, 1, 2, 3, 4, 5]

    # Every read/write in a normal setup, including errors after an MMIO store.
    peer, channel = start()
    first = len(peer.trace)
    assert call(peer, channel, policy)[0] == 0
    trace = peer.trace[first:]
    io_faults = 0
    for index, event in enumerate(trace):
        for after in ((False, True) if event[0] == 'write' else (False,)):
            peer, channel = start()
            peer.fail_at = len(peer.trace)+index
            peer.fail_after_write = after
            code, result = call(peer, channel, policy)
            assert code == -errno.EIO and channel.state == FAILED and not result.configured
            io_faults += 1

    rejected = 0
    for invalid in ('version', 'major', 'minor', 'partial', 'feature0', 'state', 'sequence', 'pending'):
        peer, channel = start()
        version = Version(valid=1, major=1, minor=5)
        candidate = Policy.from_buffer_copy(policy)
        expected = -errno.EINVAL
        if invalid == 'version': version.valid = 0
        if invalid == 'major': version.major, expected = 2, -errno.EOPNOTSUPP
        if invalid == 'minor': version.minor, expected = 4, -errno.EOPNOTSUPP
        if invalid == 'partial': candidate.boot_tables_valid = 0
        if invalid == 'feature0': candidate.feature_mask, expected = 1, -errno.EOPNOTSUPP
        if invalid == 'state': channel.state, expected = 0, -errno.EBUSY
        if invalid == 'sequence': channel.next_sequence = 1023
        if invalid == 'pending': channel.pending_sequence = 2
        result = SetupResult()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = len(peer.trace), bytes(result), bytes(channel), bytes(candidate)
        assert call(peer, channel, candidate, version, result)[0] == expected
        assert (len(peer.trace), bytes(result), bytes(channel), bytes(candidate)) == before
        rejected += 1
    for index in range(8):
        peer, channel = start()
        version, result = Version(valid=1, major=1, minor=5), SetupResult()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        args = [ctypes.byref(peer.io), ctypes.byref(channel), ctypes.byref(version),
                ctypes.byref(policy), 4, 10, ctypes.byref(result)]
        if index < 4: args[index] = None
        elif index == 4: args[6] = None
        elif index == 5: args[4] = 0
        elif index == 6: args[4] = 5002
        else:
            io = IO(None, peer.io.write32, type(peer.io.read32)(), peer.io.delay_us)
            args[0] = ctypes.byref(io)
        before = len(peer.trace), bytes(result), bytes(channel)
        assert lib.n1x_hfrp_configure_policy(*args) == -errno.EINVAL
        assert (len(peer.trace), bytes(result), bytes(channel)) == before
        rejected += 1
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'packet_comparisons': packet_cases, 'command_failures': failures,
                      'io_faults': io_faults, 'preflight_rejections': rejected,
                      'limitations': ['Reply sizes are inferred host contracts, not captures from this P16',
                                      'The peer simulates all device responses and MMIO effects',
                                      'Policy values and VF curve are synthetic; no real calibration, power-on or firmware upload']}, indent=2))


if __name__ == '__main__':
    main()
