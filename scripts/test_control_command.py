#!/usr/bin/env python3
"""Exercise the full C control transaction against faults and original HAL-1 code."""
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
from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as regs
from test_core_control import IO, READ, WRITE, DELAY
from test_response import Response
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH


class Result(ctypes.Structure):
    _fields_ = [('response', Response), ('method_attempted', ctypes.c_uint8),
                ('response_valid', ctypes.c_uint8)]


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-control-') as tmp:
        library = Path(tmp)/'control.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_control.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    control = lib.n1x_dla_control_command
    control.argtypes = [ctypes.POINTER(IO), ctypes.c_uint8, ctypes.c_uint64,
                        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Result)]

    def synthetic(*, fail_at=None, stale=False, busy=0, response_delay=0,
                  word=0x1602, data=HOST >> 8, invalid=None, polls=4):
        trace = []
        sent = False
        checks = 0
        values = {8: 128 if stale else 0, 0x40: word, 0x44: data}

        @READ
        def read(_, offset, out):
            nonlocal checks, busy
            if offset == 0x68:
                out[0] = 1 << 16 if busy else 0
                busy = max(0, busy-1)
            elif offset == 8 and sent:
                out[0] = 128 if checks >= response_delay else 0
                checks += 1
            else:
                out[0] = values[offset]
            trace.append(('read', offset, out[0]))
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @WRITE
        def write(_, offset, value):
            nonlocal sent
            trace.append(('write', offset, value))
            if offset == 0x68:
                sent = True
            return -errno.EIO if len(trace)-1 == fail_at else 0

        @DELAY
        def delay(_, interval):
            trace.append(('delay', interval))

        io = IO(None, write, read, delay)
        result = Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        io_arg, result_arg = ctypes.byref(io), ctypes.byref(result)
        command, address, interval = 22, HOST, 1
        if invalid == 'io': io_arg = None
        if invalid == 'result': result_arg = None
        if invalid == 'read': io.read32 = READ()
        if invalid == 'write': io.write32 = WRITE()
        if invalid == 'delay': io.delay_us = DELAY()
        if invalid == 'interval': interval = 0
        if invalid == 'task': command = 7
        if invalid == 'unknown_command': command = 255
        if invalid == 'zero_address': address = 0
        if invalid == 'unaligned_address': address = HOST+1
        if invalid == 'large_address': address = 1 << 40
        if invalid == 'zero_polls': polls = 0
        if invalid == 'large_polls': polls = 5002
        ret = control(io_arg, command, address, polls, interval, result_arg)
        if invalid:
            assert ret == -errno.EINVAL and not trace and bytes(result) == before
        return ret, result, trace

    ret, result, baseline = synthetic()
    assert ret == 0 and result.method_attempted and result.response_valid
    assert (result.response.word, result.response.data) == (0x1602, HOST >> 8)
    assert baseline[-3:] == [('write', 0xd24, 128), ('write', 4, 128), ('write', 0x40, ACK)]
    for position in range(len(baseline)):
        ret, result, trace = synthetic(fail_at=position)
        assert ret == -errno.EIO and trace == baseline[:position+1]
        assert result.method_attempted == (position > 0) and not result.response_valid
    ret, result, trace = synthetic(stale=True)
    assert ret == -errno.EBUSY and len(trace) == 1 and not result.method_attempted
    ret, result, _ = synthetic(busy=4)
    assert ret == -errno.ETIMEDOUT and result.method_attempted and not result.response_valid
    ret, result, _ = synthetic(response_delay=4)
    assert ret == -errno.ETIMEDOUT and result.method_attempted and not result.response_valid
    assert synthetic(busy=2, response_delay=2)[0] == 0
    for word, data, expected in ((0x1502, HOST >> 8, -errno.EPROTO),
                                  (0x1602, (HOST >> 8)+1, -errno.EPROTO),
                                  (0x80001602, HOST >> 8, -errno.EPROTO),
                                  (0x111601, HOST >> 8, -errno.EREMOTEIO),
                                  (0x1601, HOST >> 8, -errno.EPROTO)):
        ret, result, _ = synthetic(word=word, data=data)
        assert ret == expected and result.response_valid
        assert (result.response.word, result.response.data) == (word, data)
    invalids = ('io', 'result', 'read', 'write', 'delay', 'interval', 'task',
                'unknown_command', 'zero_address', 'unaligned_address',
                'large_address', 'zero_polls', 'large_polls')
    for invalid in invalids:
        synthetic(invalid=invalid)
    # A single poll needs no delay callback and must never invoke one.
    assert synthetic(polls=1)[0] == 0

    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    firmware_cases = []
    for command in (11, 21, 22, 23, 24):
        payload = bytearray(4096)
        if command == 11:
            payload[:16] = struct.pack('<IQI', 5, HOST+256, 256)
        elif command == 21:
            payload[:252] = bytes([2])+bytes(i & 255 for i in range(1, 252))
        elif command == 23:
            payload[:40] = bytes([1, 1])+struct.pack('<I', 8000)+bytes(34)
        state = {}
        trace = []

        @READ
        def read(_, offset, out):
            out[0] = (struct.unpack('<I', state['u'].mem_read(MMIO+offset, 4))[0]
                      if 'u' in state else 0)
            trace.append(('read', offset, out[0]))
            return 0

        @WRITE
        def write(_, offset, value):
            trace.append(('write', offset, value))
            if offset == 0x6c:
                state['data'] = value
            elif offset == 0x68:
                assert value == 0x300 | command and state['data'] == HOST >> 8

                def submit(u):
                    state['before_regs'] = [u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32)]
                    u.mem_write(MMIO+0x68, struct.pack('<II', value, state['data']))

                def pause(u):
                    state['u'] = u
                    emitting_pc = u.reg_read(regs.UC_RISCV_REG_PC)

                    def after_store(_, address, size, user):
                        if address == emitting_pc:
                            return
                        u.hook_del(handle)
                        u.emu_stop()

                    # Stop AFTER the IRQ-raising store committed. Stopping in
                    # its memory hook would replay it on resume, spuriously
                    # reasserting pending after the host has cleared/ACKed it.
                    handle = u.hook_add(UC_HOOK_CODE, after_store)

                state['firmware'] = run(blob, host_payload=payload, host_submit=submit,
                                        host_handler=pause, boot_from_reset=True,
                                        via_trap=True, dma_register_model=True)
                assert not state['firmware']['returned'] and 'u' in state
            else:
                u = state['u']
                if offset in (0xd24, 4):
                    target = 0xd28 if offset == 0xd24 else 8
                    old = struct.unpack('<I', u.mem_read(MMIO+target, 4))[0]
                    u.mem_write(MMIO+target, struct.pack('<I', old & ~value))
                else:
                    assert offset == 0x40 and value == ACK
                    u.mem_write(MMIO+offset, struct.pack('<I', value))
            return 0

        io, result = IO(None, write, read, DELAY()), Result()
        assert control(ctypes.byref(io), command, HOST, 1, 0, ctypes.byref(result)) == 0
        assert result.method_attempted and result.response_valid
        assert (result.response.word, result.response.data) == (command << 8 | 2, HOST >> 8)
        u, fw = state['u'], state['firmware']
        assert fw['function_stubs'] == [] and fw['firmware_code_unmodified']
        assert fw['trap_returns'] == []  # ACK alone is not ISR-return proof.
        u.emu_start(u.reg_read(regs.UC_RISCV_REG_PC), 0x40000000, count=200000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == 0x40000000
        assert fw['trap_returns'] == [0x100324]
        assert state['before_regs'] == [u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32)]
        assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
        assert not struct.unpack('<I', u.mem_read(MMIO+8, 4))[0] & 128
        if command in (21, 23):
            local, length = (0x186154, 252) if command == 21 else (0x186254, 40)
            assert bytes(u.mem_read(local, length)) == payload[:length]
        elif command in (22, 24):
            local, length = (0x186154, 252) if command == 22 else (0x186254, 40)
            assert bytes(u.mem_read(HOST, length)) == bytes(u.mem_read(local, length))
            assert bytes(u.mem_read(HOST+length, 4096-length)) == payload[length:]
        firmware_cases.append({'command': command, 'response': hex(result.response.word),
                               'original_trap_return_after_c_ack': True,
                               'dma_operations': len(fw['dma_commands'])})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'io_fault_positions': len(baseline), 'invalid_inputs': len(invalids),
                      'original_firmware_cases': firmware_cases,
                      'limitations': ['Software MMIO/FIFO/DMA/IRQ effects are explicitly modeled',
                                      'Raw HAL-1 identity is not established for opaque SROOT image',
                                      'Host C ACK and original mret are separate observed stages',
                                      'No physical firmware boot, DMA coherency, complete initialization or inference']}, indent=2))


if __name__ == '__main__':
    main()
