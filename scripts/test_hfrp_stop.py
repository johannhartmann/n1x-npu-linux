#!/usr/bin/env python3
"""Fault-test actual stop/confirmation C transport, compared to Windows policy."""
import ctypes as C
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import pefile
from test_hfrp_channel import IO, Channel, Result, Peer, READY, FAILED, NO_SEQUENCE
from test_hfrp_startup import Version
from test_hfrp_power_state import State, windows as windows_state
from test_hfrp_power_policy import run as windows
from extract_firmware import KMD_SHA256

ROOT = Path(__file__).resolve().parents[1]


class Stop(C.Structure):
    _fields_ = [('power', Result), ('query', Result), ('state', State),
                ('attempted', C.c_uint8), ('accepted', C.c_uint8), ('confirmed', C.c_uint8)]


class StopPeer(Peer):
    def __init__(self, *, off=b'\x43\0', state=b'\x07', query_status=0, **kw):
        super().__init__(**kw)
        self.off, self.state_reply, self.query_status = off, state, query_status

    def enqueue(self, sequence, status=0, payload=None):
        command = (int.from_bytes(self.packets[-1][:4], 'little') >> 18) & 1023
        assert command in (400, 431)
        super().enqueue(sequence, self.query_status if command == 431 else status,
                        self.state_reply if command == 431 else self.off)


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-stop-') as tmp:
        so = Path(tmp)/'stop.so'
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_hfrp.c'), '-o', str(so)], check=True)
        lib = C.CDLL(str(so))
    fn = lib.n1x_hfrp_stop
    fn.argtypes = [C.POINTER(IO), C.POINTER(Channel), C.POINTER(Version), C.c_uint32,
                   C.c_uint32, C.POINTER(Stop)]

    def call(peer, *, channel=None, version=None, polls=4):
        channel = channel if channel is not None else Channel(state=READY, next_sequence=1, pending_sequence=NO_SEQUENCE)
        version = version if version is not None else Version(valid=1, major=1, minor=5)
        out = Stop()
        C.memset(C.byref(out), 0xa5, C.sizeof(out))
        code = fn(C.byref(peer.io), C.byref(channel), C.byref(version), polls, 1000, C.byref(out))
        return code, out, channel

    peer = StopPeer()
    code, out, channel = call(peer)
    assert code == 0 and out.attempted and out.accepted and out.confirmed and out.state.raw == 7
    assert len(peer.packets) == 2 and peer.packets[0][4:] == b'\x43\0' and len(peer.packets[1]) == 4
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    for minor in (5, 6):
        row = windows(pe, request=0x70, enabled=0x777, version=(1, minor))
        assert [e['payload'] for e in row['events'] if 'command' in e] == ['4300']
        assert call(StopPeer(), version=Version(valid=1, major=1, minor=minor))[0] == 0
    failures = 0
    # Every transport access fails independently, including write-then-error.
    for after in (False, True):
        for i in range(len(peer.trace)):
            bus = StopPeer()
            bus.fail_at, bus.fail_after_write = i, after
            code, result, _ = call(bus)
            assert code < 0 and not result.confirmed, (after, i, code)
            failures += 1
    for state in range(256):
        code, result, _ = call(StopPeer(state=bytes([state])))
        gated = windows_state(pe, state)[1] == 7
        assert (code == 0) == gated and bool(result.confirmed) == gated
        assert result.state.raw == state  # Preserve upper bits without interpreting them.
        failures += not gated
    for options, expected in (
        ({'off': b''}, -errno.EBADMSG), ({'off': b'\x43'}, -errno.EBADMSG),
        ({'off': b'\x43\0\0'}, -errno.EBADMSG), ({'off': b'\x8c\0'}, -errno.EBADMSG),
        ({'state': b''}, -errno.EBADMSG), ({'state': b'\x07\0'}, -errno.EBADMSG),
        ({'status': 1}, -errno.EREMOTEIO), ({'query_status': 1}, -errno.EREMOTEIO),
        ({'never_reply': True}, -errno.ETIMEDOUT), ({'wrong': True}, -errno.EPROTO),
        ({'duplicate': True}, -errno.EPROTO)):
        bus = StopPeer(**options)
        code, result, _ = call(bus)
        assert code == expected and not result.confirmed, (options, code)
        failures += 1
    for version in (Version(), Version(valid=1, major=1, minor=3), Version(valid=1, major=2, minor=5)):
        bus = StopPeer()
        code, result, _ = call(bus, version=version)
        assert code < 0 and not bus.trace and bytes(result) == b'\xa5'*C.sizeof(Stop)
    for state in (0, 2, FAILED):
        bus = StopPeer()
        code, result, _ = call(bus, channel=Channel(state=state, next_sequence=1, pending_sequence=NO_SEQUENCE))
        assert code < 0 and not bus.trace and bytes(result) == b'\xa5'*C.sizeof(Stop)
    print(json.dumps(dict(passed=True, hardware_access=False, injected_failures=failures,
                         windows_v15_v16_payload='4300', confirmed_gating_bits=7, original_query_comparisons=256)))


if __name__ == '__main__':
    main()
