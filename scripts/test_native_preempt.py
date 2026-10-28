#!/usr/bin/env python3
"""Native preemption transport against original RV32 firmware and modeled IRQs."""
import ctypes as C
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
from unicorn import riscv_const as rv

from emulate_profile1_commands import ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from test_core_control import IO, READ, WRITE, DELAY
from test_native_context import start_native
from test_task_descriptor import Args
from test_profile1_lpwr_consumer import execute as idle_consumer


class Snapshot(C.Structure):
    _fields_ = [('checkpoint', C.c_uint64), ('task_fence', C.c_uint32),
                ('preempt_fence', C.c_uint32)]


class Response(C.Structure):
    _fields_ = [('word', C.c_uint32), ('data', C.c_uint32)]


class Result(C.Structure):
    _fields_ = [('response', Response), ('snapshot', Snapshot),
                ('attempted', C.c_uint32), ('response_valid', C.c_uint32),
                ('idle_notifications', C.c_uint32), ('outcome', C.c_int)]


SNAPSHOT = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(Snapshot))


class Ops(C.Structure):
    _fields_ = [('context', C.c_void_p), ('snapshot', SNAPSHOT)]


def library(directory):
    path = directory/'preempt.so'
    subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
        '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
        *[str(ROOT/'driver'/('n1x_dla_'+name+'.c'))
          for name in ('preempt', 'task', 'command', 'response')], '-o', str(path)], check=True)
    lib = C.CDLL(str(path))
    lib.n1x_dla_preempt.argtypes = [C.POINTER(IO), C.POINTER(Ops), C.c_uint64,
                                   C.c_uint32, C.c_uint32, C.POINTER(Result)]
    lib.n1x_dla_build_context_fenced_task_v2.argtypes = [C.c_void_p, C.c_size_t,
        C.POINTER(Args), C.c_uint64, C.c_uint32, C.c_uint64, C.c_uint8]
    lib.n1x_dla_build_context_header.argtypes = [C.c_void_p, C.c_size_t, C.c_uint64, C.c_uint8]
    lib.n1x_dla_build_preempt_request.argtypes = [C.c_void_p, C.c_size_t, C.c_uint64,
                                               C.c_uint64, C.c_uint32, C.c_uint32]
    lib.n1x_dla_build_address_list.argtypes = [C.c_void_p, C.c_size_t,
                                             C.POINTER(C.c_uint64), C.c_uint16]
    return lib


def firmware_case(lib, firmware, model, mode, irq):
    state = {'active': False, 'sent': False, 'started': False, 'paused': False, 'step': 0}
    errors, trace, execution = [], [], []

    def response(u):
        if not state['active']:
            return False
        pc = u.reg_read(rv.UC_RISCV_REG_PC)
        size = 4 if int.from_bytes(u.mem_read(pc, 2), 'little') & 3 == 3 else 2
        handle = []

        def pause(_, address, length, data):
            u.hook_del(handle[0])
            state['paused'] = True
            u.emu_stop()

        handle.append(u.hook_add(UC_HOOK_CODE, pause, begin=pc+size, end=pc+size))
        return True  # The native response consumer must ACK this notification.

    f = start_native(lib, None, firmware, model, response_handler=response,
                     enabled=mode != 'disabled')
    u, word = f['u'], f['word']
    state['active'] = True
    u.mem_write(MMIO+0x25044, struct.pack('<I', 0 if mode == 'save_fail' else 1))

    def returns(_, pc, size, data):
        value = u.reg_read(rv.UC_RISCV_REG_A0)
        execution.append(value if value < 1 << 31 else value-(1 << 32))

    u.hook_add(UC_HOOK_CODE, returns, begin=0x101b1c, end=0x101b1c)

    def interrupt(bits):
        assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        assert word(MMIO+0xd28) & bits
        u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | bits))
        status = u.reg_read(rv.UC_RISCV_REG_MSTATUS)
        u.reg_write(rv.UC_RISCV_REG_MSTATUS,
                    (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
        u.reg_write(rv.UC_RISCV_REG_MCAUSE, 0x8000000b)
        u.reg_write(rv.UC_RISCV_REG_MEPC, u.reg_read(rv.UC_RISCV_REG_RA))
        u.emu_start(u.reg_read(rv.UC_RISCV_REG_MTVEC), 0x40000000, count=200000)
        assert state['paused'] or u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194

    def advance():
        if not state['sent'] or word(MMIO+8) & 128:
            return
        if state['paused']:
            state['paused'] = False
            u.emu_start(u.reg_read(rv.UC_RISCV_REG_PC), 0x40000000, count=200000)
            assert u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        elif not state['started']:
            state['started'] = True
            if mode == 'finished_before':
                # Task completes after native preflight but before method delivery.
                u.mem_write(MMIO+0x3054, struct.pack('<I', 1))
                interrupt(irq)
            u.mem_write(MMIO+0x64, struct.pack('<I', word(MMIO+0x6c)))
            interrupt(4)
        else:
            statuses = {'save': (4, 0, 0), 'save_fail': (4,),
                        'finished': (1,), 'finished5': (5,)}.get(mode, ())
            if state['step'] < len(statuses):
                u.mem_write(MMIO+0x3054, struct.pack('<I', statuses[state['step']]))
                state['step'] += 1
                interrupt(irq)

    @READ
    def read(_, offset, out):
        try:
            if offset == 8:
                advance()
            out[0] = word(MMIO+offset)
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @WRITE
    def write(_, offset, value):
        try:
            trace.append([offset, value])
            if offset in (4, 0xd24):
                target = 8 if offset == 4 else 0xd28
                u.mem_write(MMIO+target, struct.pack('<I', word(MMIO+target) & ~value))
            else:
                u.mem_write(MMIO+offset, struct.pack('<I', value))
            if offset == 0x68:
                state['sent'] = True
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @DELAY
    def delay(_, interval):
        assert interval == 1000

    @SNAPSHOT
    def snapshot(_, out):
        try:
            out[0] = Snapshot(int.from_bytes(u.mem_read(f['context_buffer']+24, 8), 'little'),
                              word(HOST+0x6060), word(HOST+f['fence']))
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    io, ops, result = IO(None, write, read, delay), Ops(None, snapshot), Result()
    status = lib.n1x_dla_preempt(C.byref(io), C.byref(ops), HOST+f['header'], 1, 8, C.byref(result))
    assert not errors, errors
    expected = {'save': (0, 1, [0]), 'finished': (0, 2, [0]), 'finished5': (0, 2, [0]),
                'finished_before': (0, 2, [0]),
                'save_fail': (-errno.EREMOTEIO, 0, [-16]),
                'disabled': (-errno.EREMOTEIO, 0, []), 'timeout': (-errno.ETIMEDOUT, 0, [])}[mode]
    assert (status, result.outcome, execution) == expected, (mode, status, result.outcome, execution)
    assert result.attempted == 1
    assert [t for t in trace if t[0] == 0x68] == [[0x68, 0x212]]
    if mode != 'timeout':
        assert result.response_valid == 1
        assert trace[-3:] == [[0xd24, 128], [4, 128], [0x40, ACK]]
        assert result.response.word == (0xc1201 if mode == 'disabled' else 0x120b)
        assert state['paused']  # Native ACK issued, firmware has not consumed it yet.
        advance()
        assert not state['paused']
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    assert bytes(u.mem_read(f['layout'][1]['address'], 3136)) == f['input']
    assert bytes(u.mem_read(f['layout'][2]['address'], 3136)) == b'\xa5'*3136
    assert len(f['result']['task_interrupts']) == int(mode == 'finished_before')
    return {'mode': mode, 'engine_irq': hex(irq), 'status': status, 'outcome': result.outcome,
            'executor_status': execution, 'notifications': f['notifications'],
            'native_writes': trace, 'firmware_code_unmodified': True, 'output_still_poisoned': True}


def fault_cases(lib, idle):
    """Bounded waits, stale DMA, malformed responses and every I/O failure edge."""
    count = 0

    def run(*, initial=(0, 0, 0xa5a5a5a5), terminal=(1, 0, 8),
            messages=((0x120b, 0),), pending=0, busy=False, fail=None, dma=0x1000,
            task=1, missing=None):
        state = {'sent': False, 'snapshots': 0, 'delays': 0}
        frames, trace = list(messages), []

        def access(kind, *args):
            trace.append((kind, *args))
            return -errno.EIO if len(trace)-1 == fail else 0

        @READ
        def read(_, offset, out):
            err = access('read', offset)
            if err:
                return err
            if offset == 8:
                out[0] = (128 if frames else 0) if state['sent'] else pending
            elif offset == 0x68:
                out[0] = 1 << 16 if busy else 0
            elif offset in (0x40, 0x44):
                out[0] = frames[0][int(offset == 0x44)]
            else:
                raise AssertionError(offset)
            return 0

        @WRITE
        def write(_, offset, value):
            err = access('write', offset, value)
            if err:
                return err
            if offset == 0x68:
                state['sent'] = True
            elif offset == 0x40:
                assert value == ACK
                frames.pop(0)
            return 0

        @DELAY
        def delay(_, interval):
            assert interval == 1000
            state['delays'] += 1

        @SNAPSHOT
        def snapshot(_, out):
            err = access('snapshot')
            if err:
                return err
            out[0] = Snapshot(*(initial if not state['snapshots'] else terminal))
            state['snapshots'] += 1
            return 0

        io = IO(None, WRITE() if missing == 'write' else write,
                READ() if missing == 'read' else read, DELAY() if missing == 'delay' else delay)
        ops = Ops(None, SNAPSHOT() if missing == 'snapshot' else snapshot)
        result = Result()
        C.memset(C.byref(result), 0xa5, C.sizeof(result))
        rc = lib.n1x_dla_preempt(None if missing == 'io' else C.byref(io),
                                 None if missing == 'ops' else C.byref(ops), dma, task, 8,
                                 None if missing == 'result' else C.byref(result))
        return rc, result, trace, state

    rc, result, baseline, _ = run()
    assert rc == 0 and result.outcome == 1 and result.attempted == result.response_valid == 1
    count += 1
    for index in range(len(baseline)):
        rc, result, trace, _ = run(fail=index)
        assert rc == -errno.EIO and result.outcome == 0
        assert trace == baseline[:index+1], (index, trace)
        if baseline[index] == ('write', 0x40, ACK):
            assert result.response_valid == 0  # ACK failed; no successful capture.
        count += 1
    for missing in ('io', 'ops', 'result', 'read', 'write', 'delay', 'snapshot'):
        rc, result, trace, _ = run(missing=missing)
        assert rc == -errno.EINVAL and not trace
        if missing != 'result':
            assert result.attempted == result.outcome == 0
        count += 1
    for dma in (0, 257, (1 << 36)-256, 1 << 36):
        rc, result, trace, _ = run(dma=dma)
        assert rc == -errno.EINVAL and result.attempted == 0 and not trace
        count += 1
    rc, result, trace, _ = run(task=0)
    assert rc == -errno.EINVAL and not trace
    count += 1
    for initial, status in (((1, 0, 0), errno.ESTALE), ((0, 0, 8), errno.ESTALE),
                             ((0, 1, 0), errno.EALREADY)):
        rc, result, trace, _ = run(initial=initial)
        assert rc == -status and result.attempted == 0 and trace == [('snapshot',)]
        count += 1
    for pending in (64, 128, 192):
        rc, result, trace, _ = run(pending=pending)
        assert rc == -errno.EBUSY and result.attempted == 0
        assert trace == [('snapshot',), ('read', 8)]
        count += 1
    for terminal, status, outcome in (((0, 1, 8), 0, 2), ((1, 1, 8), errno.EPROTO, 0),
                                     ((2, 0, 8), errno.EPROTO, 0), ((0, 0, 8), errno.EREMOTEIO, 0),
                                     ((1, 0, 5), errno.EPROTO, 0), ((1 << 32, 0, 8), errno.EPROTO, 0)):
        rc, result, _, _ = run(terminal=terminal)
        assert rc == -status and result.outcome == outcome and result.response_valid == 1
        count += 1
    for word, data, status in ((0x120b, 1, errno.EPROTO), (0x1202, 0x10, errno.EPROTO),
                              (0x1120b, 0, errno.EREMOTEIO), (0x100120b, 0, errno.EPROTO),
                              (0x1201, 0x10, errno.EREMOTEIO), (0x708, 0, errno.EREMOTEIO),
                              (0x709, 0, errno.EREMOTEIO), (0x70a, 0, errno.EREMOTEIO),
                              (12, 1, errno.EPROTO), (0x12, 0, errno.EPROTO),
                              (0x99, 0, errno.EPROTO)):
        rc, result, _, _ = run(messages=((word, data),))
        assert rc == -status and result.outcome == 0 and result.response_valid == 1
        count += 1
    rc, result, _, _ = run(messages=(idle, idle, (0x120b, 0)))
    assert rc == 0 and result.outcome == 1 and result.idle_notifications == 2
    count += 1
    for kwargs in ({'messages': ()}, {'busy': True}, {'messages': (idle,)*5001}):
        rc, result, trace, state = run(**kwargs)
        assert rc == -errno.ETIMEDOUT and result.outcome == 0 and result.attempted == 1
        assert state['delays'] == 5000
        assert sum(t[0] == 'write' and t[1] == 0x68 for t in trace) == int(not kwargs.get('busy'))
        count += 1
    return count


def main():
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    # Execute the original idle producer with a synthetic timer. Its outbound
    # notification boundary is intercepted by idle_consumer, not guessed here.
    idle_events, notified = idle_consumer(firmware, 1, 0, 1)
    assert idle_events == [[12, 0]] and notified == 1
    with tempfile.TemporaryDirectory(prefix='n1x-preempt-') as tmp:
        lib = library(Path(tmp))
        faults = fault_cases(lib, tuple(idle_events[0]))
        cases = [firmware_case(lib, firmware, model, mode, irq)
                 for irq in (0x400, 0x800)
                 for mode in ('save', 'save_fail', 'finished', 'finished5', 'finished_before',
                              'disabled', 'timeout')]
    print(json.dumps({'passed': True, 'hardware_access': False, 'fault_cases': faults, 'cases': cases,
                      'firmware_sha256': PROFILE1_HASH,
                      'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'idle_notification_from_firmware': idle_events[0],
                      'limitations': ['Original firmware; synthetic engine events, MMIO and RAM DMA',
                                      'No arithmetic, physical checkpoint transfer or DMA quiescence proof',
                                      'Idle producer uses synthetic timer and intercepted outbound boundary',
                                      'Private native primitive, not wired into Linux EXEC']}, indent=2))


if __name__ == '__main__':
    main()
