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
from emulate_firmware_commands import run as firmware, BLOB_HASH, MMIO, HOST, ACK
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
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_response.c'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    take = lib.n1x_dla_take_response
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Response)]
    send = lib.n1x_dla_send_method
    send.argtypes = [ctypes.POINTER(IO)]+[ctypes.c_uint32]*4

    def host(uc=None, word=0x102, data=0x200010, pending=True, fail_at=None, modeled_irq=False):
        trace = []
        values = {8: 0x80 if pending else 0, 0x40: word, 0x44: data}

        @READ
        def read(_, offset, out):
            trace.append(('read', offset))
            if len(trace)-1 == fail_at:
                return -errno.EIO
            out[0] = struct.unpack('<I', uc.mem_read(MMIO+offset, 4))[0] if uc and (offset != 8 or modeled_irq) else values[offset]
            return 0

        @WRITE
        def write(_, offset, value):
            trace.append(('write', offset, value))
            if len(trace)-1 == fail_at:
                return -errno.EIO
            if uc and modeled_irq and offset in (0xd24, 4):
                target = 0xd28 if offset == 0xd24 else 8
                previous = struct.unpack('<I', uc.mem_read(MMIO+target, 4))[0]
                uc.mem_write(MMIO+target, struct.pack('<I', previous & ~value))
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
    submissions = []

    def submit(uc):
        trace = []

        @READ
        def read(_, offset, out):
            assert offset == 0x68
            out[0] = struct.unpack('<I', uc.mem_read(MMIO+offset, 4))[0]
            trace.append(('read', offset))
            return 0

        @WRITE
        def write(_, offset, value):
            assert offset in (0x68, 0x6c)
            uc.mem_write(MMIO+offset, struct.pack('<I', value))
            trace.append(('write', offset, value))
            return 0

        io = IO(None, write, read, DELAY())
        assert send(ctypes.byref(io), 0x301, HOST >> 8, 1, 0) == 0
        assert trace == [('read', 0x68), ('write', 0x6c, HOST >> 8), ('write', 0x68, 0x301)]
        submissions.append(trace)

    sequence = [1, 0x11111111, 0x40000000, 3]
    captured = []

    def masked_interrupt(uc):
        assert struct.unpack('<I', uc.mem_read(MMIO+0xd28, 4))[0] & 0x80
        captured.append(host(uc, modeled_irq=True))
        assert struct.unpack('<I', uc.mem_read(MMIO+0xd28, 4))[0] == 0
        assert struct.unpack('<I', uc.mem_read(MMIO+8, 4))[0] == 0

    result = firmware_profile1(profile1, sequence=sequence, host_handler=masked_interrupt, host_submit=submit)
    assert len(submissions) == 4
    assert result['returned'] and result['completed'] == [4, 0x44444444, 0x40000000, 12]
    assert len(captured) == 4 and all(row[0] == 0 and row[1] == expected for row in captured)
    assert [row[2][0] for row in captured] == [0x102, 0x102, 0x110101, 0x102]
    assert result['final_irq_mask'] == result['final_irq_pending'] == 0
    assert result['final_mailboxes'] == (0x11223344, 0x55667788)
    irq_writes = [row for row in result['register_writes'] if row[0] in (0xd20, 0)]
    assert irq_writes == [(0xd20, 0x80), (0, 0x80)]*4
    negative = firmware_profile1(profile1, sequence=sequence, suppress_mask_set=True,
                                 host_handler=masked_interrupt)
    assert not negative['returned'] and not negative['interrupts'] and not negative['completed']
    assert len(captured) == 4  # Masked request cannot reach the host handler.
    assert negative['final_irq_mask'] == 0 and negative['final_irq_pending'] == 0x80
    captured.clear()
    submissions.clear()

    def initialized_interrupt(uc):
        assert struct.unpack('<I', uc.mem_read(MMIO+0xd28, 4))[0] == 0x1f85
        captured.append(host(uc, modeled_irq=True))
        assert struct.unpack('<I', uc.mem_read(MMIO+0xd28, 4))[0] == 0x1f05
        assert struct.unpack('<I', uc.mem_read(MMIO+8, 4))[0] == 0
        assert struct.unpack('<I', uc.mem_read(0x1862bc, 4))[0] == 0x180100

    initialized = firmware_profile1(profile1, sequence=sequence, host_handler=initialized_interrupt,
                                    host_submit=submit, boot_from_reset=True)
    assert initialized['boot_state']['scheduler_wfi']
    assert initialized['returned'] and initialized['completed'] == [4, 0x44444444, 0x40000000, 12]
    assert len(submissions) == len(captured) == 4
    assert all(row[0] == 0 and row[1] == expected for row in captured)
    assert [row[2][0] for row in captured] == [0x102, 0x102, 0x110101, 0x102]
    assert initialized['final_irq_mask'] == 0x1f05 and initialized['final_irq_pending'] == 0
    assert initialized['final_mailboxes'] == (0x11223344, 0x55667788)
    print(json.dumps({'scope': 'Linux C response capture/clear/ACK with synthetic IRQSTAT and real FW mailbox code',
                      'ordered_callback_count': len(expected),
                      'persistent_hal1_commands': len(sequence),
                      'commands_after_original_reset_initialization': len(sequence),
                      'initialized_command_stubs': ['DMA read', 'DMA write'],
                      'firmware_rearms_swgen1_before_each_reply': True,
                      'persistent_commands_use_linux_method_sender': True,
                      'suppressed_mask_set_blocks_delivery': True, 'error_prefixes_checked': 6,
                      'spurious_irq_has_no_writes': True, 'integrated_firmware_cases': integrated,
                      'limitations': ['IRQSTAT and routing are synthetic',
                                      'No kernel IRQ registration, physical MMIO or DMA was used',
                                      'Command completion is not inference completion'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
