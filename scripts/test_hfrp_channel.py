#!/usr/bin/env python3
"""Exercise real C HFRP handshake/ownership against a bounded synthetic peer."""
import ctypes
import errno
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

from test_hfrp_ring import ROOT, IO, READ, WRITE, DELAY, Tx, Reply

ASYNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(Reply))
NEW, READY, ACTIVE, FAILED = range(4)
NO_SEQUENCE = 0xffff


class Channel(ctypes.Structure):
    _fields_ = [('state', ctypes.c_int), ('next_sequence', ctypes.c_uint16),
                ('pending_sequence', ctypes.c_uint16), ('async_context', ctypes.c_void_p),
                ('async_reply', ASYNC)]


class Result(ctypes.Structure):
    _fields_ = [('tx', Tx), ('reply', Reply), ('async_count', ctypes.c_uint32),
                ('discarded_startup_replies', ctypes.c_uint32), ('reply_valid', ctypes.c_uint8),
                ('reset_attempted', ctypes.c_uint8), ('reset_completed', ctypes.c_uint8),
                ('peer_ready', ctypes.c_uint8), ('ack_attempted', ctypes.c_uint8),
                ('ack_completed', ctypes.c_uint8), ('mode_select_attempted', ctypes.c_uint8),
                ('mode_selected', ctypes.c_uint8)]


class Peer:
    def __init__(self, *, reset_delay=0, ready_delay=0, reply_delay=0, status=0,
                 startup=False, async_before=False, async_after=False, wrong=False,
                 duplicate=False, never_reset=False, never_ready=False, never_reply=False):
        self.ram = bytearray(0x200)
        self.trace, self.packets, self.delays, self.errors = [], [], [], []
        self.pending_reply = None
        self.reset_delay, self.ready_delay, self.reply_delay = reset_delay, ready_delay, reply_delay
        self.reset_ticks = self.ready_ticks = self.reply_ticks = 0
        self.resetting = self.waiting_ready = False
        self.status, self.startup = status, startup
        self.async_before, self.async_after, self.wrong, self.duplicate = async_before, async_after, wrong, duplicate
        self.never_reset, self.never_ready, self.never_reply = never_reset, never_ready, never_reply
        self.fail_at, self.fail_after_write = None, False
        self.access_error = -errno.EIO
        self.on_ack = None
        self.read_cb, self.write_cb, self.delay_cb = READ(self.read), WRITE(self.write), DELAY(self.delay)
        self.io = IO(None, self.write_cb, self.read_cb, self.delay_cb)

    def word(self, offset):
        return struct.unpack_from('<I', self.ram, offset)[0]

    def put(self, offset, value):
        struct.pack_into('<I', self.ram, offset, value)

    def enqueue(self, sequence, status=0, payload=b'\x01\x02\x03\x04'):
        head, tail = self.ram[0x188], self.ram[0x111]
        packet = struct.pack('<I', len(payload)+4 | (sequence << 8) | (status << 18))+payload
        if (head-tail)%116+len(packet) >= 116:
            self.errors.append('modeled response ring overflow')
            return
        for byte in packet:
            self.ram[0x18c+head] = byte
            head = (head+1)%116
        self.ram[0x188] = head
        self.put(0x104, self.word(0x104) | 2)

    def pump(self):
        if self.resetting and self.reset_ticks >= self.reset_delay and not self.never_reset:
            self.put(0x100, 0)
            self.resetting = False
            self.waiting_ready = True
        if self.waiting_ready and self.ready_ticks >= self.ready_delay and not self.never_ready:
            self.put(0x104, self.word(0x104) | 2)
            self.waiting_ready = False
            if self.startup:
                self.enqueue(0x3ff)
        if self.pending_reply is not None and self.reply_ticks >= self.reply_delay and not self.never_reply:
            sequence = self.pending_reply
            self.pending_reply = None
            if self.async_before:
                self.enqueue(0x3ff, payload=b'\xaa')
            self.enqueue((sequence+1)%1023 if self.wrong else sequence, self.status)
            if self.duplicate:
                self.enqueue(sequence, self.status)
            if self.async_after:
                self.enqueue(0x3ff, payload=b'\xbb')

    def read(self, _, offset, pointer):
        if offset%4 or offset+4 > len(self.ram):
            self.errors.append(('bad read', offset))
            return -errno.EFAULT
        self.pump()
        value = self.word(offset)
        self.trace.append(('read', offset, value))
        if len(self.trace)-1 == self.fail_at:
            return self.access_error
        pointer[0] = value
        return 0

    def write(self, _, offset, value):
        if offset%4 or offset+4 > len(self.ram):
            self.errors.append(('bad write', offset))
            return -errno.EFAULT
        self.trace.append(('write', offset, value))
        failed = len(self.trace)-1 == self.fail_at
        if failed and not self.fail_after_write:
            return self.access_error
        if offset == 0x100 and value == 1:
            if self.word(4) != 1:
                self.errors.append('reset before circular-mode selection')
                return -errno.EPROTO
            self.put(0x100, 1)
            self.put(0x104, 0)
            self.put(0x110, 0)
            self.put(0x188, 0)
            self.resetting = True
            self.pump()
        elif offset == 0x100 and value == 2:
            head, tail = self.ram[0x110], self.ram[0x189]
            packet = bytearray()
            while tail != head:
                packet.append(self.ram[0x114+tail])
                tail = (tail+1)%116
            if len(packet) < 4 or len(packet) != packet[0]:
                self.errors.append(('invalid outgoing packet', packet.hex()))
                return -errno.EPROTO
            header = struct.unpack_from('<I', packet)[0]
            self.packets.append(bytes(packet))
            self.ram[0x189] = head
            self.pending_reply = (header >> 8) & 1023
            self.reply_ticks = 0
            self.pump()
        elif offset == 0x10c:
            if value != 2:
                self.errors.append(('unexpected IRQ clear', value))
            self.put(0x104, self.word(0x104) & ~value)
            if self.on_ack:
                action, self.on_ack = self.on_ack, None
                action()
        else:
            self.put(offset, value)
        return self.access_error if failed else 0

    def delay(self, _, interval):
        self.delays.append(interval)
        if self.resetting:
            self.reset_ticks += 1
        elif self.waiting_ready:
            self.ready_ticks += 1
        elif self.pending_reply is not None:
            self.reply_ticks += 1
        self.pump()


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-channel-') as tmp:
        library = Path(tmp)/'channel.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_hfrp.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    lib.n1x_hfrp_handshake.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Channel),
                                      ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Result)]
    lib.n1x_hfrp_exchange.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Channel), ctypes.c_uint16,
                                     ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Result)]
    cases = []

    def handshake(peer, channel=None, polls=4):
        channel = channel if channel is not None else Channel()
        result = Result()
        code = lib.n1x_hfrp_handshake(ctypes.byref(peer.io), ctypes.byref(channel), polls, 10, ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, channel, result

    def exchange(peer, channel, command=999, payload=b'', polls=4):
        result = Result()
        data = (ctypes.c_uint8*len(payload)).from_buffer_copy(payload)
        code = lib.n1x_hfrp_exchange(ctypes.byref(peer.io), ctypes.byref(channel), command,
                                   data, len(payload), polls, 10, ctypes.byref(result))
        assert not peer.errors, peer.errors
        return code, result

    def failed_gate(peer, channel):
        before, state = len(peer.trace), bytes(channel)
        assert exchange(peer, channel)[0] == -errno.ENOTRECOVERABLE
        assert handshake(peer, channel)[0] == -errno.ENOTRECOVERABLE
        assert len(peer.trace) == before and bytes(channel) == state

    for options in ({}, {'reset_delay': 2}, {'ready_delay': 2}, {'reply_delay': 2}, {'startup': True},
                    {'reset_delay': 3, 'ready_delay': 3, 'reply_delay': 3}):
        peer = Peer(**options)
        code, channel, result = handshake(peer)
        assert code == 0 and channel.state == READY and channel.pending_sequence == NO_SEQUENCE
        assert channel.next_sequence == 2 and result.reply_valid and result.reply.sequence == 1
        assert result.reset_completed and result.peer_ready and result.ack_completed
        assert result.mode_selected and peer.trace[0] == ('write', 4, 1)
        assert result.discarded_startup_replies == int(options.get('startup', False))
        assert peer.packets == [bytes.fromhex('090104000000ccdd00')]
        before = len(peer.trace)
        assert handshake(peer, channel)[0] == -errno.EALREADY and len(peer.trace) == before
        code, response = exchange(peer, channel)
        assert code == 0 and response.reply.sequence == 2 and response.reply.length == 4
        assert peer.packets[-1] == struct.pack('<I', 4 | (2 << 8) | (999 << 18))
        cases.append({'case': 'success', 'options': options})
    for options, expected in (({'never_reset': True}, -errno.ETIMEDOUT),
                              ({'never_ready': True}, -errno.ETIMEDOUT),
                              ({'never_reply': True}, -errno.ETIMEDOUT),
                              ({'reply_delay': 4}, -errno.ETIMEDOUT),
                              ({'status': 0x123}, -errno.EREMOTEIO),
                              ({'wrong': True}, -errno.EPROTO),
                              ({'duplicate': True}, -errno.EPROTO)):
        peer = Peer(**options)
        code, channel, result = handshake(peer)
        assert code == expected and channel.state == FAILED
        if options.get('never_reply'):
            assert channel.pending_sequence == 1 and len(peer.packets) == 1
        if options.get('never_reset') or options.get('never_ready'):
            assert not peer.packets
        failed_gate(peer, channel)
        cases.append({'case': 'handshake_failure', 'options': options, 'status': code})
    # A confirmed command-level failure does not lose transport ownership.
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.status = 0x3ff
    code, result = exchange(peer, channel)
    assert code == -errno.EREMOTEIO and result.reply_valid and result.reply.status == 0x3ff
    assert channel.state == READY and channel.pending_sequence == NO_SEQUENCE
    peer.status = 0
    assert exchange(peer, channel)[0] == 0
    channel.next_sequence = 1022
    assert exchange(peer, channel)[0] == 0 and channel.next_sequence == 0
    assert exchange(peer, channel)[0] == 0 and channel.next_sequence == 1
    cases.append({'case': 'confirmed_error_and_sequence_wrap'})
    # Timeout prevents reusing the sequence, including after a delayed reply arrives.
    peer.never_reply = True
    code, result = exchange(peer, channel)
    assert code == -errno.ETIMEDOUT and channel.state == FAILED and channel.pending_sequence == 1
    peer.never_reply = False
    peer.pump()
    failed_gate(peer, channel)
    cases.append({'case': 'late_reply_cannot_reopen_failed_channel'})
    # Async notifications need an explicit consumer, including arrivals around ACK.
    for disposition in ('handled', 'missing', 'remote_error', 'space_error', 'ack_race'):
        peer = Peer()
        _, channel, _ = handshake(peer)
        delivered = []
        @ASYNC
        def consume(_, response):
            delivered.append(bytes(response.contents.payload[:response.contents.length]))
            before = len(peer.trace)
            if exchange(peer, channel)[0] != -errno.EBUSY or len(peer.trace) != before:
                peer.errors.append('async callback reentered the active transport')
                return -errno.EIO
            return -errno.EREMOTEIO if disposition == 'remote_error' else -errno.ENOSPC if disposition == 'space_error' else 0
        if disposition != 'missing':
            channel.async_reply = consume
        if disposition == 'ack_race':
            peer.on_ack = lambda: peer.enqueue(1023, payload=b'\xcc')
        else:
            peer.async_before = peer.async_after = True
        code, result = exchange(peer, channel)
        if disposition in ('handled', 'ack_race'):
            assert code == 0 and channel.state == READY
            assert delivered == ([b'\xaa', b'\xbb'] if disposition == 'handled' else [b'\xcc'])
            assert result.async_count == len(delivered)
        else:
            expected = {'missing': -errno.EOPNOTSUPP, 'remote_error': -errno.EREMOTEIO,
                        'space_error': -errno.ENOSPC}[disposition]
            assert code == expected and channel.state == FAILED and not result.reply_valid
            failed_gate(peer, channel)
        cases.append({'case': 'async', 'disposition': disposition})
    # Stale synchronous responses fail before a new request is published.
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.enqueue(channel.next_sequence)
    before = len(peer.packets)
    assert exchange(peer, channel)[0] == -errno.EPROTO and channel.state == FAILED
    assert len(peer.packets) == before
    cases.append({'case': 'stale_reply_before_send'})
    # Full ring is safe to retry, because no command head was published.
    peer = Peer()
    _, channel, _ = handshake(peer)
    head = peer.ram[0x110]
    peer.ram[0x189] = (head-115)%116
    before = len(peer.packets)
    code, result = exchange(peer, channel)
    assert code == -errno.ENOSPC and channel.state == READY and not result.tx.head_publish_attempted
    assert result.tx.space_unavailable
    assert len(peer.packets) == before and channel.pending_sequence == NO_SEQUENCE
    peer.ram[0x189] = head
    assert exchange(peer, channel)[0] == 0
    cases.append({'case': 'full_ring_retry'})
    # Interrupt bits unrelated to the reply doorbell must remain untouched.
    peer.put(0x104, 0xfc000000)
    assert exchange(peer, channel)[0] == 0 and peer.word(0x104) == 0xfc000000
    cases.append({'case': 'preserve_other_irq_bits'})
    # Notification failure before publication is not confused with a full ring.
    peer = Peer()
    _, channel, _ = handshake(peer)
    @ASYNC
    def no_space(_, response):
        return -errno.ENOSPC
    channel.async_reply = no_space
    peer.enqueue(1023, payload=b'\xaa')
    before = len(peer.packets)
    assert exchange(peer, channel)[0] == -errno.ENOSPC and channel.state == FAILED
    assert len(peer.packets) == before
    cases.append({'case': 'preflight_async_error_is_fatal'})
    # A peer replenishing async messages cannot make a polling pass unbounded.
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.async_before = True
    delivered = []
    @ASYNC
    def replenish(_, response):
        delivered.append(1)
        peer.enqueue(1023, payload=b'\xaa')
        return 0
    channel.async_reply = replenish
    assert exchange(peer, channel)[0] == -errno.EOVERFLOW and channel.state == FAILED
    assert len(delivered) == 28
    cases.append({'case': 'async_flood_bounded', 'callbacks': len(delivered)})
    for total, expected in ((3, -errno.EPROTO), (8, -errno.EOVERFLOW), (55, -errno.EMSGSIZE)):
        peer = Peer()
        _, channel, _ = handshake(peer)
        head = peer.ram[0x188]
        for byte in struct.pack('<I', total | (2 << 8)):
            peer.ram[0x18c+head] = byte
            head = (head+1)%116
        peer.ram[0x188] = head
        tail, before = peer.ram[0x111], len(peer.packets)
        assert exchange(peer, channel)[0] == expected and channel.state == FAILED
        assert peer.ram[0x111] == tail and len(peer.packets) == before
        cases.append({'case': 'malformed_preflight', 'header_size': total})
    # Capture a reply arriving during the ACK race window, after the first drain.
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.never_reply = True
    before = len(peer.packets)
    def arrive_during_ack():
        if len(peer.packets) == before:
            peer.on_ack = arrive_during_ack
        else:
            peer.enqueue(channel.pending_sequence)
            peer.pending_reply = None
    peer.on_ack = arrive_during_ack
    assert exchange(peer, channel)[0] == 0 and channel.state == READY
    cases.append({'case': 'matching_reply_arrives_during_ack'})
    peer = Peer()
    peer.io.delay_us = DELAY()
    assert handshake(peer, polls=1)[0] == 0 and not peer.delays
    cases.append({'case': 'single_poll_without_delay_callback'})
    # Same errno from an accessor is not a proven full-ring condition.
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.trace.clear()
    assert exchange(peer, channel)[0] == 0
    # First command-buffer read occurs after preflight and capacity checks.
    point = next(i for i, event in enumerate(peer.trace) if event[0] == 'read' and 0x114 <= event[1] < 0x188)
    peer = Peer()
    _, channel, _ = handshake(peer)
    peer.trace.clear()
    peer.fail_at, peer.access_error = point, -errno.ENOSPC
    code, result = exchange(peer, channel)
    assert code == -errno.ENOSPC and not result.tx.space_unavailable and channel.state == FAILED
    cases.append({'case': 'accessor_enospc_is_fatal'})
    fault_count = 0
    for phase in ('handshake', 'exchange'):
        baseline = Peer()
        _, base_channel, _ = handshake(baseline)
        if phase == 'exchange':
            baseline.trace.clear()
            assert exchange(baseline, base_channel)[0] == 0
        operations = baseline.trace
        for index, operation in enumerate(operations):
            for after in (False, True) if operation[0] == 'write' else (False,):
                peer = Peer()
                channel = Channel()
                if phase == 'exchange':
                    assert handshake(peer, channel)[0] == 0
                    peer.trace.clear()
                peer.fail_at, peer.fail_after_write = index, after
                if phase == 'handshake':
                    code, channel, result = handshake(peer, channel)
                else:
                    code, result = exchange(peer, channel)
                assert code == -errno.EIO and channel.state == FAILED, (phase, index, after, code)
                assert len(peer.trace) == index+1 and peer.trace == operations[:index+1]
                if result.tx.head_publish_attempted:
                    assert channel.pending_sequence != NO_SEQUENCE
                failed_gate(peer, channel)
                fault_count += 1
    # State and invalid-input gates cause no I/O and preserve output sentinels.
    invalid_count = 0
    for state in (NEW, ACTIVE, FAILED):
        peer, channel = Peer(), Channel()
        channel.state = state
        result = Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        code = lib.n1x_hfrp_exchange(ctypes.byref(peer.io), ctypes.byref(channel), 999,
                                    None, 0, 4, 10, ctypes.byref(result))
        assert code == (-errno.ENOTRECOVERABLE if state == FAILED else -errno.EBUSY)
        assert bytes(result) == before and not peer.trace
        invalid_count += 1
    for polls, command, length in ((0, 999, 0), (5002, 999, 0), (4, 1024, 0), (4, 999, 112), (4, 999, 1)):
        peer = Peer()
        _, channel, _ = handshake(peer)
        peer.trace.clear()
        result = Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before, state = bytes(result), bytes(channel)
        assert lib.n1x_hfrp_exchange(ctypes.byref(peer.io), ctypes.byref(channel), command,
                                      None, length, polls, 10, ctypes.byref(result)) == -errno.EINVAL
        assert bytes(result) == before and bytes(channel) == state and not peer.trace
        invalid_count += 1
    for operation in ('handshake', 'exchange'):
        for missing in ('io', 'read', 'write', 'delay', 'channel', 'result'):
            peer = Peer()
            channel = Channel()
            if operation == 'exchange':
                assert handshake(peer, channel)[0] == 0
                peer.trace.clear()
            if missing == 'read': peer.io.read32 = READ()
            if missing == 'write': peer.io.write32 = WRITE()
            if missing == 'delay': peer.io.delay_us = DELAY()
            result = Result()
            ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
            before, state = bytes(result), bytes(channel)
            args = (None if missing == 'io' else ctypes.byref(peer.io),
                    None if missing == 'channel' else ctypes.byref(channel))
            output = None if missing == 'result' else ctypes.byref(result)
            code = (lib.n1x_hfrp_handshake(*args, 4, 10, output) if operation == 'handshake' else
                    lib.n1x_hfrp_exchange(*args, 999, None, 0, 4, 10, output))
            assert code == -errno.EINVAL and bytes(result) == before and bytes(channel) == state
            assert not peer.trace
            invalid_count += 1
    for next_sequence, pending in ((1023, NO_SEQUENCE), (2, 3)):
        peer = Peer()
        _, channel, _ = handshake(peer)
        peer.trace.clear()
        channel.next_sequence, channel.pending_sequence = next_sequence, pending
        state = bytes(channel)
        assert exchange(peer, channel)[0] == -errno.EINVAL and bytes(channel) == state and not peer.trace
        invalid_count += 1
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': cases,
                      'access_failure_cases': fault_count, 'invalid_state_argument_cases': invalid_count,
                      'limitations': ['Synthetic peer; no physical handshake, reset or power transition',
                                      'Other IRQ bits preserved for a future owning device driver',
                                      'Failed channels require separately established hardware recovery; no automatic reset']}, indent=2))


if __name__ == '__main__':
    main()
