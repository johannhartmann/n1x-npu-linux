#!/usr/bin/env python3
"""Test Linux response handling, including an original-FW ping/ACK round trip."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import struct
import subprocess
import tempfile
from pathlib import Path
from emulate_firmware_commands import run as firmware, BLOB_HASH, MMIO, ACK
from emulate_profile1_commands import run as firmware_profile1, PROFILE1_HASH
from test_core_control import IO, WRITE, READ, DELAY

ROOT = Path(__file__).resolve().parents[1]


class Response(ctypes.Structure):
    _fields_ = [('word', ctypes.c_uint32), ('data', ctypes.c_uint32)]


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-dla-response-') as tmp:
        library = Path(tmp)/'response.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    take = lib.n1x_dla_take_response
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Response)]

    def host(uc=None, word=0x102, data=0x200010, pending=True, fail_at=None):
        trace = []
        values = {8: 0x80 if pending else 0, 0x40: word, 0x44: data}

        @READ
        def read(_, offset, out):
            trace.append(('read', offset))
            if len(trace)-1 == fail_at:
                return -errno.EIO
            out[0] = struct.unpack('<I', uc.mem_read(MMIO+offset, 4))[0] if uc and offset != 8 else values[offset]
            return 0

        @WRITE
        def write(_, offset, value):
            trace.append(('write', offset, value))
            if len(trace)-1 == fail_at:
                return -errno.EIO
            if uc and offset == 0x40:
                uc.mem_write(MMIO+offset, struct.pack('<I', value))
            return 0

        io = IO(None, write, read, DELAY())
        response = Response(0xa5a5a5a5, 0x5a5a5a5a)
        status = take(ctypes.byref(io), ctypes.byref(response))
        return status, trace, (response.word, response.data)

    expected = [('read', 8), ('read', 0x40), ('read', 0x44),
                ('write', 0xd24, 0x80), ('write', 4, 0x80), ('write', 0x40, ACK)]
    assert host() == (0, expected, (0x102, 0x200010))
    assert host(word=0xffffffff, data=0x12345678) == (0, expected, (0xffffffff, 0x12345678))
    for position in range(len(expected)):
        status, trace, result = host(fail_at=position)
        assert status == -errno.EIO and trace == expected[:position+1]
        assert result == (0xa5a5a5a5, 0x5a5a5a5a)
    assert host(pending=False) == (-errno.EAGAIN, expected[:1], (0xa5a5a5a5, 0x5a5a5a5a))
    assert take(None, None) == -errno.EINVAL
    integrated = []
    profile1 = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(profile1).hexdigest() == PROFILE1_HASH
    for label, execute, image in (('T35x RV64', firmware, blob),
                                   ('HAL 1 RV32', firmware_profile1, profile1)):
        for challenge, expected_word in ((1, 0x102), (0x11111111, 0x102), (0x40000000, 0x110101)):
            captured = []

            def interrupt(uc):
                captured.append(host(uc))

            result = execute(image, challenge=challenge, host_handler=interrupt)
            assert result['returned'] and len(captured) == 1
            status, trace, (word, data) = captured[0]
            assert status == 0 and trace == expected and word == expected_word
            assert (word, data) == result['interrupts'][0]
            assert result['final_mailboxes'] == (0x11223344, 0x55667788)
            integrated.append({'image': label, 'challenge': hex(challenge), 'response': hex(word),
                               'firmware_returned_after_linux_ack': True})
    print(json.dumps({'scope': 'Linux C response capture/clear/ACK with synthetic IRQSTAT and real FW mailbox code',
                      'ordered_callback_count': len(expected), 'error_prefixes_checked': 6,
                      'spurious_irq_has_no_writes': True, 'integrated_firmware_cases': integrated,
                      'limitations': ['IRQSTAT and routing are synthetic',
                                      'No kernel IRQ registration, physical MMIO or DMA was used',
                                      'Command completion is not inference completion'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
