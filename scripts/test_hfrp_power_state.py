#!/usr/bin/env python3
"""Compare native HFRP state query with original ARM64; all I/O is RAM-only."""
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
from unicorn import UC_HOOK_CODE

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import BASE, PARENT, OWNER, ROOT, KMD_SHA256
from test_hfrp_channel import (IO, READ, WRITE, DELAY, Peer, Channel, Result,
                               READY, NEW, ACTIVE, FAILED, NO_SEQUENCE)
from test_hfrp_startup import Version


class State(ctypes.Structure):
    _fields_ = [('raw', ctypes.c_uint8), ('valid', ctypes.c_uint8)]


class StatePeer(Peer):
    def __init__(self, payload=b'\x07', **kwargs):
        super().__init__(**kwargs)
        self.payload = payload

    def enqueue(self, sequence, status=0, payload=None):
        super().enqueue(sequence, status, self.payload if payload is None else payload)


def windows(pe, raw, *, version=(1, 5), status=0, error=0, length=1):
    m = DispatchMachine(pe)
    hfrp, provider, link, output = (OWNER+i*0x1000 for i in range(4))
    for ptr, value in ((PARENT+0x300, hfrp), (hfrp+0x60, provider),
                       (provider+0x18, link)):
        m.write(ptr, 'Q', value)
    m.write(link+8, 'I', 1)
    m.write(link+0xc, 'BB', *version)
    calls = []

    def hook(_, pc, size, user):
        if pc == BASE+0xd3de30:
            assert [m.reg(i) for i in range(4)] == [hfrp, 0x1af, 0, 0]
            calls.append(0x1af)
            m.write(m.reg(4), 'H', status)
            if length:
                m.write(m.reg(5), 'B', raw)
            m.write(m.reg(6), 'I', length)
            m.ret(error)

    m.u.hook_add(UC_HOOK_CODE, hook)
    code = m.call(0xd41630, PARENT, output)
    return code, m.read(output, 'I')[0], calls


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-state-') as tmp:
        library = Path(tmp)/'state.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_hfrp.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    ptr = ctypes.POINTER
    query = lib.n1x_hfrp_query_power_state
    query.argtypes = [ptr(IO), ptr(Channel), ptr(Version), ctypes.c_uint32,
                      ctypes.c_uint32, ptr(State), ptr(Result)]
    version = Version(valid=1, major=1, minor=5)

    def ready():
        # The channel's handshake/ownership is established separately. This
        # fixture starts READY so every recorded write belongs to the query.
        return Channel(state=READY, next_sequence=1, pending_sequence=NO_SEQUENCE)

    def call(peer, channel, version=version, polls=4):
        state, result = State(0xff, 1), Result()
        code = query(ctypes.byref(peer.io), ctypes.byref(channel), ctypes.byref(version),
                     polls, 10, ctypes.byref(state), ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, state, result

    for raw in range(256):
        peer, channel = StatePeer(bytes([raw])), ready()
        code, state, result = call(peer, channel)
        assert code == 0 and state.valid == 1 and state.raw == raw
        assert windows(pe, raw) == (0, state.raw & 7, [0x1af])
        assert peer.packets == [struct.pack('<I', 4 | (1 << 8) | (0x1af << 18))]
        assert result.reply_valid and result.ack_completed and channel.state == READY
        assert channel.pending_sequence == NO_SEQUENCE and channel.next_sequence == 2
        assert not any(e[:2] in (('write', 4), ('write', 0x100)) and e[2] == 1
                       for e in peer.trace)  # no mode select or transport reset

    failures = []
    for size in (0, 2, 3, 50):
        peer, channel = StatePeer(bytes(size)), ready()
        code, state, result = call(peer, channel)
        assert code == -errno.EBADMSG and bytes(state) == b'\0\0'
        assert result.reply_valid and result.reply.length == size and channel.state == FAILED
        before = len(peer.trace)
        assert call(peer, channel)[0] == -errno.ENOTRECOVERABLE and len(peer.trace) == before
        failures.append({'reply_bytes': size, 'status': code})
    # Windows accepts an empty reply as zero state. Native rejects that case.
    assert windows(pe, 0, length=0) == (0, 0, [0x1af])
    for options, expected, next_state in (({'status': 1}, -errno.EREMOTEIO, READY),
                                          ({'never_reply': True}, -errno.ETIMEDOUT, FAILED),
                                          ({'wrong': True}, -errno.EPROTO, FAILED),
                                          ({'duplicate': True}, -errno.EPROTO, FAILED)):
        peer, channel = StatePeer(**options), ready()
        code, state, result = call(peer, channel)
        assert code == expected and bytes(state) == b'\0\0' and channel.state == next_state
        assert len(peer.packets) == 1
        if options.get('never_reply'):
            assert peer.delays == [10]*3 and channel.pending_sequence == 1
        failures.append({'injected': options, 'status': code})
    assert windows(pe, 7, status=1) == (0xffff, 0, [0x1af])
    assert windows(pe, 7, error=0x65) == (0x65, 0, [0x1af])
    peer, channel = StatePeer(), ready()
    peer.ram[0x189] = 1  # 115 of 116 bytes occupied: no room for the query.
    code, state, result = call(peer, channel)
    assert code == -errno.ENOSPC and bytes(state) == b'\0\0'
    assert channel.state == READY and channel.pending_sequence == NO_SEQUENCE
    assert not peer.packets and result.tx.space_unavailable and not result.tx.head_publish_attempted
    failures.append({'full_command_ring': True, 'status': code})

    baseline, channel = StatePeer(), ready()
    assert call(baseline, channel)[0] == 0
    faults = 0
    for index, operation in enumerate(baseline.trace):
        for after in ((False, True) if operation[0] == 'write' else (False,)):
            peer, channel = StatePeer(), ready()
            peer.fail_at, peer.fail_after_write = index, after
            code, state, result = call(peer, channel)
            assert code == -errno.EIO and bytes(state) == b'\0\0' and channel.state == FAILED
            assert peer.trace == baseline.trace[:index+1]
            faults += 1

    rejected = 0
    for case in ('io', 'read', 'write', 'delay', 'channel', 'version', 'state', 'result',
                 'invalid_version', 'old_version', 'new_major', 'new', 'active', 'failed',
                 'zero_polls', 'too_many_polls', 'sequence', 'pending'):
        peer, channel = StatePeer(), ready()
        ver, state, result = Version(valid=1, major=1, minor=5), State(0xff, 1), Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        polls, expected = 4, -errno.EINVAL
        if case == 'read': peer.io.read32 = READ()
        if case == 'write': peer.io.write32 = WRITE()
        if case == 'delay': peer.io.delay_us = DELAY()
        if case == 'invalid_version': ver.valid = 0
        if case == 'old_version': ver.minor, expected = 3, -errno.EOPNOTSUPP
        if case == 'new_major': ver.major, expected = 2, -errno.EOPNOTSUPP
        if case in ('new', 'active', 'failed'):
            channel.state = {'new': NEW, 'active': ACTIVE, 'failed': FAILED}[case]
            expected = -errno.ENOTRECOVERABLE if case == 'failed' else -errno.EBUSY
        if case == 'zero_polls': polls = 0
        if case == 'too_many_polls': polls = 5002
        if case == 'sequence': channel.next_sequence = 1023
        if case == 'pending': channel.pending_sequence = 1
        before = (bytes(channel), bytes(state), bytes(result))
        code = query(None if case == 'io' else ctypes.byref(peer.io),
            None if case == 'channel' else ctypes.byref(channel),
            None if case == 'version' else ctypes.byref(ver), polls, 10,
            None if case == 'state' else ctypes.byref(state),
            None if case == 'result' else ctypes.byref(result))
        assert code == expected, (case, code)
        assert before == (bytes(channel), bytes(state), bytes(result)) and not peer.trace
        rejected += 1
    for minor in (5, 6, 255):
        peer, channel = StatePeer(b'\x85'), ready()
        code, state, _ = call(peer, channel, Version(valid=1, major=1, minor=minor))
        assert code == 0 and state.raw == 0x85
        assert windows(pe, 0x85, version=(1, minor)) == (0, 5, [0x1af])
    assert windows(pe, 7, version=(1, 3)) == (0x56, 0, [])
    assert windows(pe, 7, version=(2, 5)) == (0x56, 0, [])
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
        'original_entry': '0xd41630', 'original_response_byte_comparisons': 256,
        'failures': failures, 'access_failure_cases': faults, 'argument_state_rejections': rejected,
        'limitations': ['Synthetic READY transport, firmware replies and MMIO; original query sender is a stub',
            'Raw bits do not prove physical power state, engine idle or DMA retirement',
            'No live query, automatic startup/recovery integration or reset is enabled']}, indent=2))


if __name__ == '__main__':
    main()
