#!/usr/bin/env python3
"""Execute the complete original Windows mailbox wrapper and Linux C exchange."""
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
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_READ
from test_core_control import IO, READ, WRITE, DELAY
from test_sroot_send import ROOT, BASE, HASH
from test_sroot_receive import Reply
from emulate_sroot_image import machine, call, CTX, REQUEST, INPUT, BUFFER, MEMORY

MMIO = BUFFER+0x8000


class Bus:
    def __init__(self, stride, reply_header, reply_words, busy=0, stale=0):
        self.stride, self.reply = stride, [reply_header]+reply_words
        self.busy, self.stale = busy, stale
        self.trace, self.tx = [], []
        self.echo_index = self.rx_index = 0
        self.submitted = self.echo_ready = False
        self.suppress = None

    def read(self, offset):
        index = offset//self.stride
        assert offset % self.stride == 0
        if index == self.suppress:
            value = 0
        elif index == 2:
            value = 1
        elif index == 5:
            value = int(self.stale > 0 or self.submitted)
        elif index == 8:
            value = int(self.echo_ready)
        elif index == 11:
            value = self.busy
        elif index == 7:
            value = self.tx[self.echo_index] ^ 0xffffffff
            self.echo_index += 1
        elif index == 4:
            value = self.reply[self.rx_index]
            self.rx_index += 1
        else:
            raise AssertionError(('unexpected read', index))
        self.trace.append(('r', offset, value))
        return value

    def write(self, offset, value):
        index = offset//self.stride
        assert offset % self.stride == 0
        if index == 1:
            self.tx.append(value)
        elif index == 3:
            assert value in (1, 3)
            if value == 1:
                self.submitted = self.echo_ready = True
        elif index == 6:
            assert value == 1
            if self.stale:
                self.stale -= 1
            else:
                self.submitted = False
        elif index == 9:
            assert value == 1
            self.echo_ready = False
        else:
            assert index == 12 and value == 0xffffffff
        self.trace.append(('w', offset, value))


def windows(pe, bus, command, words, reply_count):
    u = machine(pe)
    u.mem_write(CTX+0x50, struct.pack('<I', bus.stride))
    u.mem_write(CTX+0x58, struct.pack('<Q', MMIO))
    u.mem_write(REQUEST+8, bytes([command]))
    u.mem_write(REQUEST+0x10, struct.pack('<Q', INPUT if words else 0))
    u.mem_write(REQUEST+0x18, bytes([len(words)]))
    u.mem_write(REQUEST+0x20, struct.pack('<Q', BUFFER if reply_count else 0))
    u.mem_write(REQUEST+0x28, bytes([reply_count]))
    u.mem_write(REQUEST+0x38, struct.pack('<Q', CTX))
    u.mem_write(REQUEST+0x40, struct.pack('<Q', MEMORY))
    u.mem_write(INPUT, struct.pack('<'+'I'*len(words), *words))

    def memory(_, access, address, size, value, user):
        assert size == 4
        if access == UC_MEM_READ:
            value = bus.read(address-MMIO)
            u.mem_write(address, struct.pack('<I', value))
        else:
            bus.write(address-MMIO, value)

    u.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory, begin=MMIO, end=MMIO+0xfff)
    result = call(u, 0x5580, (REQUEST,))
    status_word = struct.unpack('<I', u.mem_read(MEMORY, 4))[0]
    copied = list(struct.unpack('<'+'I'*reply_count, u.mem_read(BUFFER, 4*reply_count)))
    return result, status_word, copied


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-exchange-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    exchange = lib.n1x_sroot_exchange
    exchange.argtypes = [ctypes.POINTER(IO), ctypes.c_uint32, ctypes.c_uint8,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint8, ctypes.POINTER(Reply),
                         ctypes.c_uint32, ctypes.c_uint32]

    def linux(bus, command, words, fail_at=None, polls=4, no_reply=False):
        reply = Reply()
        ctypes.memset(ctypes.byref(reply), 0xa5, ctypes.sizeof(reply))
        before = bytes(reply)

        @READ
        def read(_, offset, output):
            output[0] = bus.read(offset)
            return -errno.EIO if len(bus.trace)-1 == fail_at else 0

        @WRITE
        def write(_, offset, value):
            bus.write(offset, value)
            return -errno.EIO if len(bus.trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            bus.trace.append(('d', interval))

        io = IO(None, write, read, delay)
        data = (ctypes.c_uint32*len(words))(*words)
        result = exchange(ctypes.byref(io), bus.stride, command, data, len(words),
                          None if no_reply else ctypes.byref(reply), polls, 1)
        if result:
            assert bytes(reply) == before
            return result, None
        return result, (reply.header, reply.command, reply.status, reply.detail,
                        list(reply.words[:reply.count]))

    cases = 0
    for stride in (4, 16):
        for tx_count, rx_count in ((0, 0), (1, 1), (3, 3), (14, 14)):
            for status, detail in ((0, 0), (1, 0), (0, 0x80)):
                for stale in (0, 2):
                    words = [0xe0010000+i for i in range(tx_count)]
                    responses = [0x87654321+i for i in range(rx_count)]
                    header = 0x77 | rx_count << 8 | status << 12 | detail << 24
                    original, candidate = [Bus(stride, header, responses, stale=stale) for _ in range(2)]
                    returned, decoded_status, copied = windows(pe, original, 0x18, words, rx_count)
                    assert returned == 0 and decoded_status == status | detail << 12
                    assert copied == (responses if not status else [0]*rx_count)
                    assert linux(candidate, 0x18, words) == (0, (header, 0x77, status, detail, responses))
                    assert [event for event in candidate.trace if event[0] != 'd'] == original.trace
                    cases += 1
    words = [0xc0302, 67508, 0xe0010000]
    base = Bus(16, 0x18, [])
    assert linux(base, 0x18, words)[0] == 0
    for i in range(len(base.trace)):
        bus = Bus(16, 0x18, [])
        assert linux(bus, 0x18, words, fail_at=i) == (-errno.EIO, None)
        assert bus.trace == base.trace[:i+1]
    busy = Bus(16, 0x18, [], busy=1)
    assert windows(pe, busy, 0x18, words, 0)[0] == 7
    candidate = Bus(16, 0x18, [], busy=1)
    assert linux(candidate, 0x18, words) == (-errno.EBUSY, None)
    assert candidate.trace == busy.trace
    for suppressed in (2, 8, 5):
        bus = Bus(16, 0x18, [])
        bus.suppress = suppressed
        assert linux(bus, 0x18, words)[0] == -errno.ETIMEDOUT
        assert bus.trace[-1] == ('r', 16*suppressed, 0)
    for count in (15,):
        bus = Bus(16, 0x18, [])
        assert linux(bus, 0x18, [0]*count) == (-errno.EINVAL, None) and not bus.trace
    bus = Bus(16, 0x18, [])
    assert linux(bus, 0x18, words, no_reply=True) == (-errno.EINVAL, None) and not bus.trace
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_wrapper_rva': '0x5580',
                      'matching_complete_transactions': cases, 'callback_failure_positions': len(base.trace),
                      'limitations': ['Synthetic firmware responses, readiness, stride and DMA words',
                                      'Transport accepts replies without proving request identity or command success',
                                      'No physical backend, allocation, global deadline or inference']}, indent=2))


if __name__ == '__main__':
    main()
