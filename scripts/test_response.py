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
from test_task_descriptor import Args as TaskArgs

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
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_response.c'), str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_task.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    take = lib.n1x_dla_take_response
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Response)]
    send = lib.n1x_dla_send_method
    send.argtypes = [ctypes.POINTER(IO)]+[ctypes.c_uint32]*4
    build_task = lib.n1x_dla_build_task_v2
    build_task.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(TaskArgs)]
    active_method = 0x301

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
        assert send(ctypes.byref(io), active_method, HOST >> 8, 1, 0) == 0
        assert trace == [('read', 0x68), ('write', 0x6c, HOST >> 8), ('write', 0x68, active_method)]
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
                                    host_submit=submit, boot_from_reset=True, via_trap=True)
    assert initialized['boot_state']['scheduler_wfi']
    assert initialized['returned'] and initialized['completed'] == [4, 0x44444444, 0x40000000, 12]
    assert len(submissions) == len(captured) == 4
    assert all(row[0] == 0 and row[1] == expected for row in captured)
    assert [row[2][0] for row in captured] == [0x102, 0x102, 0x110101, 0x102]
    assert initialized['final_irq_mask'] == 0x1f05 and initialized['final_irq_pending'] == 0
    assert initialized['final_mailboxes'] == (0x11223344, 0x55667788)
    assert initialized['trap_entries'] == [0x1002dc]*4
    assert initialized['trap_returns'] == [0x100324]*4
    assert initialized['dispatches'] == [(0x301, HOST >> 8)]*4
    assert initialized['method_reads'] == [(0x68, 0x301), (0x64, HOST >> 8)]*4
    assert [item for item in initialized['register_writes'] if item[0] == 0x74] == [(0x74, 1)]*4
    stalled = firmware_profile1(profile1, sequence=sequence, host_handler=initialized_interrupt,
                                host_submit=submit, boot_from_reset=True, via_trap=True,
                                trap_dma_ready=False)
    assert not stalled['returned'] and not stalled['completed']
    assert stalled['pc'] in ('0x100266', '0x10026a', '0x10026e')
    assert stalled['trap_entries'] == [0x1002dc] and not stalled['trap_returns']
    assert stalled['dispatches'] == [(0x301, HOST >> 8)]
    assert len(stalled['interrupts']) == 1 and stalled['host_word'] == 4
    captured.clear()
    submissions.clear()
    register_dma = firmware_profile1(profile1, sequence=sequence, host_handler=initialized_interrupt,
                                     host_submit=submit, boot_from_reset=True, via_trap=True,
                                     dma_register_model=True)
    assert register_dma['function_stubs'] == [] and register_dma['firmware_code_unmodified']
    assert register_dma['returned'] and register_dma['completed'] == [4, 0x44444444, 0x40000000, 12]
    assert len(submissions) == len(captured) == 4
    assert all(row[0] == 0 and row[1] == expected for row in captured)
    assert [row[2][0] for row in captured] == [0x102, 0x102, 0x110101, 0x102]
    assert register_dma['trap_entries'] == [0x1002dc]*4
    assert register_dma['trap_returns'] == [0x100324]*4
    assert register_dma['final_irq_mask'] == 0x1f05 and register_dma['final_irq_pending'] == 0
    expected_dma = []
    for overflow in (False, False, True, False):
        expected_dma.append({'command': 0x2000, 'source': HOST, 'destination': 0x1833ac,
                             'bytes': 4, 'direction': 'read'})
        if not overflow:
            expected_dma.append({'command': 0x2020, 'source': 0x1833ac, 'destination': HOST,
                                 'bytes': 4, 'direction': 'write'})
    assert register_dma['dma_commands'] == expected_dma
    assert register_dma['final_mailboxes'] == (0x11223344, 0x55667788)
    not_ready = firmware_profile1(profile1, sequence=sequence, host_handler=initialized_interrupt,
                                  host_submit=submit, boot_from_reset=True, via_trap=True,
                                  dma_register_model=True, trap_dma_ready=False)
    assert not not_ready['returned'] and not not_ready['completed']
    assert not not_ready['dma_commands'] and not not_ready['interrupts']
    assert not_ready['host_word'] == 1 and len(captured) == 4
    assert not_ready['pc'] in ('0x100266', '0x10026a', '0x10026e')
    active_method = 0x307
    task = ctypes.create_string_buffer(256)
    task_args = TaskArgs(0, HOST+256, 1000000, 1)
    assert build_task(task, len(task), ctypes.byref(task_args)) == 0
    captured.clear()
    submissions.clear()
    submitted = firmware_profile1(profile1, method=active_method, host_payload=task.raw,
                                    host_handler=initialized_interrupt, host_submit=submit,
                                    boot_from_reset=True, via_trap=True, dma_register_model=True)
    assert submitted['returned'] and submitted['firmware_code_unmodified']
    assert submitted['function_stubs'] == [] and len(submissions) == len(captured) == 1
    assert captured[0][0:2] == (0, expected)
    slot = bytes.fromhex(submitted['first_task_slot'])
    assert captured[0][2] == (0x702, HOST >> 8)
    assert slot[:60] == task.raw[:60]
    assert struct.unpack_from('<I', slot, 124)[0] == 2
    assert struct.unpack_from('<Q', slot, 128)[0] == HOST
    queue = bytes.fromhex(submitted['queue0'])
    assert struct.unpack_from('<I', queue, 8)[0] == 0x180200
    assert struct.unpack_from('<II', queue, 16) == (2, 1)  # capacity, queued count
    chunks = submitted['dma_commands']
    assert [item['bytes'] for item in chunks] == [32, 16, 8, 4]
    position = 0
    for item in chunks:
        assert item['direction'] == 'read'
        assert item['source'] == HOST+position and item['destination'] == 0x180200+position
        position += item['bytes']
    assert position == 60 and submitted['trap_returns'] == [0x100324]
    bad_task = bytearray(task.raw)
    bad_task[8] = 255
    captured.clear()
    rejected = firmware_profile1(profile1, method=active_method, host_payload=bytes(bad_task),
                                  host_handler=initialized_interrupt, host_submit=submit,
                                  boot_from_reset=True, via_trap=True, dma_register_model=True)
    assert rejected['returned'] and rejected['firmware_code_unmodified']
    assert rejected['function_stubs'] == [] and captured[0][2] == (0x90701, HOST >> 8)
    rejected_queue = bytes.fromhex(rejected['queue0'])
    assert struct.unpack_from('<I', rejected_queue, 20)[0] == 0
    assert struct.unpack_from('<I', bytes.fromhex(rejected['first_task_slot']), 124)[0] == 0
    print(json.dumps({'scope': 'Linux C response capture/clear/ACK with synthetic IRQSTAT and real FW mailbox code',
                      'ordered_callback_count': len(expected),
                      'persistent_hal1_commands': len(sequence),
                      'commands_after_original_reset_initialization': len(sequence),
                      'commands_through_original_trap_and_mret': len(sequence),
                      'all_interrupted_integer_registers_restored': True,
                      'missing_dma_ready_prevents_isr_return': True,
                      'register_dma_command_stubs': register_dma['function_stubs'],
                      'original_dma_register_commands': len(register_dma['dma_commands']),
                      'missing_dma_idle_blocks_original_transfer': True,
                      'linux_task_queued_through_original_dma_and_trap': True,
                      'invalid_task_version_rejected_without_enqueue': True,
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
