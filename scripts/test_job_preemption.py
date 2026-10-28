#!/usr/bin/env python3
"""Native job start/preempt/resume/poll against original HAL-1 firmware."""
import ctypes as C
import errno
import hashlib
import json
from pathlib import Path
import struct
import tempfile

from unicorn import UC_HOOK_CODE
from unicorn import riscv_const as rv

from test_native_preempt import library, Snapshot, Ops, SNAPSHOT
from test_native_context import start_native
from test_core_control import IO, READ, WRITE, DELAY
from emulate_profile1_commands import ROOT, HOST, MMIO, ACK, PROFILE1_HASH

PUBLISH = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32)
FENCE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint32))


class JobOps(C.Structure):
    _fields_ = [('context', C.c_void_p), ('publish', PUBLISH), ('read_fence', FENCE)]


def build(directory):
    shim = directory/'job.c'
    shim.write_text(r'''
#include <string.h>
#include <errno.h>
#include "n1x_dla_job_control.h"
static struct n1x_dla_job_queue queue;
static struct n1x_dla_job job;
static struct n1x_dla_io io;
static struct n1x_dla_job_ops ops;
static struct n1x_dla_preempt_ops control;
static struct n1x_dla_preempt_result result;
void job_setup(const struct n1x_dla_io *a,const struct n1x_dla_job_ops *b,
               const struct n1x_dla_preempt_ops *c,__u64 dma) {
 memset(&queue,0,sizeof(queue));memset(&job,0,sizeof(job));memset(&result,0,sizeof(result));
 io=*a;ops=*b;control=*c;job.dma=dma;job.resumable=true;}
int job_start(void) {return n1x_dla_job_start(&queue,&io,&ops,&job);}
int job_poll(void) {return n1x_dla_job_poll(&queue,&io,&ops,&job);}
int job_preempt(__u64 dma,__u32 sequence) {
 return n1x_dla_job_preempt(&queue,&io,&control,&job,dma,sequence,&result);}
int job_resume(void) {return n1x_dla_job_resume(&queue,&io,&control,&job);}
int job_fail(void) {return n1x_dla_job_fail(&queue,&job,-ECANCELED);}
void job_test_polls(__u32 value) {job.polls=value;}
void job_test_resumable(int value) {job.resumable=value;}
int job_wrong(int resume) {
 struct n1x_dla_job other={.resumable=true};struct n1x_dla_preempt_result ignored;
 return resume?n1x_dla_job_resume(&queue,&io,&control,&other):
 n1x_dla_job_preempt(&queue,&io,&control,&other,256,8,&ignored);}
__u32 job_field(unsigned field) {
 switch(field){case 0:return queue.active&&queue.owner_job==&job;
 case 1:return queue.poisoned;case 2:return job.accepted;case 3:return job.suspended;
 case 4:return job.verified;case 5:return job.preempt_completed;case 6:return job.polls;
 case 7:return job.fence_value;case 8:return queue.last_fence;case 9:return job.preempt_sequence;
 case 10:return result.outcome;case 11:return result.attempted;case 12:return job.signal_seen;
 case 13:return job.restore_pending;
 default:return 0;}}
''')
    lib = library(directory, (ROOT/'driver/n1x_dla_job.c', ROOT/'driver/n1x_dla_job_control.c', shim))
    lib.job_setup.argtypes = [C.POINTER(IO), C.POINTER(JobOps), C.POINTER(Ops), C.c_uint64]
    lib.job_preempt.argtypes = [C.c_uint64, C.c_uint32]
    lib.job_field.argtypes = [C.c_uint]
    lib.job_field.restype = C.c_uint32
    return lib


def execute(lib, firmware, model, mode, irq):
    state = {'active': False, 'paused': False, 'method': None, 'events': [],
             'starts': 0, 'published': 0, 'restore_calls': 0, 'save_calls': 0, 'injected': False}
    errors, trace, executor, transitions, deferrals = [], [], [], [], []
    restore_modes = ('resume_accept', 'restore_first', 'restore_second')
    resume_faults = ('resume_marker_corrupt', 'resume_snapshot_failure', 'resume_both')
    repeat = mode == 'twice' or mode in (*restore_modes, *resume_faults)

    def response(u):
        if not state['active']:
            return False
        pc = u.reg_read(rv.UC_RISCV_REG_PC)
        size = 4 if int.from_bytes(u.mem_read(pc, 2), 'little') & 3 == 3 else 2
        handles = []

        def pause(_, address, length, data):
            u.hook_del(handles[0])
            state['paused'] = True
            u.emu_stop()

        handles.append(u.hook_add(UC_HOOK_CODE, pause, begin=pc+size, end=pc+size))
        return True

    f = start_native(lib, None, firmware, model, job_flags=128, submit_task=False,
                     response_handler=response, enabled=mode != 'disabled')
    u, word = f['u'], f['word']
    state['active'] = True
    u.mem_write(MMIO+0x25044, struct.pack('<I', 0 if mode == 'save_fail' else 1))

    def observe(_, pc, size, data):
        if pc == 0x105a6a:
            state['restore_calls'] += 1
        if pc == 0x105a08:
            state['save_calls'] += 1
        if pc == 0x101b1c:
            v = u.reg_read(rv.UC_RISCV_REG_A0)
            executor.append(v if v < 1 << 31 else v-(1 << 32))

    u.hook_add(UC_HOOK_CODE, observe)

    def interrupt(bits):
        before = word(0x1862a4)
        writes_before = len(f['result']['scheduler_mmio_writes'])
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
        transitions.append({'irq': hex(bits), 'before': before, 'after': word(0x1862a4)})
        if bits == 4 and before == 4:
            # Unlike state 1, the original handler requests no engine preempt IL.
            assert not any(w[0] in (0x3018, 0x25058)
                           for w in f['result']['scheduler_mmio_writes'][writes_before:])

    def advance():
        if word(MMIO+8) & 128:
            return
        if state['paused']:
            state['paused'] = False
            u.emu_start(u.reg_read(rv.UC_RISCV_REG_PC), 0x40000000, count=200000)
            assert state['paused'] or u.reg_read(rv.UC_RISCV_REG_PC) == 0x102194
        elif state['method'] is not None:
            method = state['method']
            state['method'] = None
            if method == 0x212:
                if mode == 'finished_before':
                    u.mem_write(MMIO+0x3054, struct.pack('<I', 1))
                    interrupt(irq)
                state['events'] = {'save_fail': [4], 'finished': [1],
                                   'finished_before': [], 'disabled': [], 'timeout': []}.get(mode, [4, 0, 0])
                if mode in restore_modes and state['starts'] > 1:
                    assert word(0x1862a4) == 4
                    # State 5 waits only for the already-issued restore record.
                    # It does not launch a new two-record engine save operation.
                    state['events'] = [0]
            else:
                assert method == 0x307
                state['starts'] += 1
                if state['starts'] > 1:
                    state['events'] = [0, 0, 1]
            u.mem_write(MMIO+0x64, struct.pack('<I', word(MMIO+0x6c)))
            interrupt(4)
        elif state['events']:
            u.mem_write(MMIO+0x3054, struct.pack('<I', state['events'].pop(0)))
            interrupt(irq)

    @READ
    def read(_, offset, out):
        try:
            if offset == 8:
                advance()
                # Explicit synthetic transport fault after the real 0x120b ACK.
                # The original firmware has finished its notification path.
                if (not state['injected'] and not state['paused'] and
                    f['notifications'][-1]['mailbox'] == [0x120b, 0]):
                    state['injected'] = True
                    if mode in ('late_error', 'idle_after'):
                        u.mem_write(MMIO+0x40, struct.pack('<II',
                            12 if mode == 'idle_after' else 0x8003070a,
                            0 if mode == 'idle_after' else HOST >> 8))
                        u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | 128))
                    if mode == 'drain_read_failure':
                        return -errno.EIO
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
                assert state['method'] is None
                state['method'] = value
                if mode == 'resume_send_failure' and value == 0x307 and state['starts']:
                    return -errno.EIO  # Write may have reached the device; never retry.
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @DELAY
    def delay(_, interval):
        if interval != 1000:
            errors.append('unexpected delay '+str(interval))

    @PUBLISH
    def publish(_, value):
        try:
            state['published'] += 1
            assert value == 1 and state['published'] == 1
            assert word(0x180114) == 0
            u.mem_write(HOST+73, struct.pack('<I', value))
            u.mem_write(HOST+0x6060, bytes(4))
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @FENCE
    def fence(_, out):
        try:
            out[0] = word(HOST+0x6060)
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @SNAPSHOT
    def snapshot(_, out):
        try:
            if mode == 'resume_snapshot_failure' and state['starts'] > 1:
                return -errno.EIO
            out[0] = Snapshot(int.from_bytes(u.mem_read(f['context_buffer']+24, 8), 'little'),
                              word(HOST+0x6060), word(HOST+f['fence']))
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    io, ops, control = IO(None, write, read, delay), JobOps(None, publish, fence), Ops(None, snapshot)
    lib.job_setup(C.byref(io), C.byref(ops), C.byref(control), HOST)
    if mode == 'not_resumable':
        lib.job_test_resumable(0)
    assert lib.job_start() == 0 and lib.job_field(0)
    assert not lib.job_field(2)
    assert lib.job_poll() == -errno.EINPROGRESS and lib.job_field(2)
    advance()  # Let original firmware finish acceptance and program the engine.
    assert not state['paused'] and word(0x1862a4) == 1
    immutable_task = bytes(u.mem_read(HOST, 256))
    before = len(trace)
    assert lib.job_wrong(0) == lib.job_wrong(1) == -errno.ENOENT and len(trace) == before
    stages = []
    for cycle in range(2 if repeat else 1):
        sequence = 8*(cycle+1)
        if cycle:
            before = len(trace)
            assert lib.job_preempt(HOST+f['header'], sequence-8) == -errno.EINVAL
            assert lib.job_field(0) and len(trace) == before
            request = C.create_string_buffer(512)
            assert lib.n1x_dla_build_preempt_request(request, 512, HOST+f['header'],
                                                    HOST+f['fence'], sequence-3, sequence) == 0
            u.mem_write(HOST+f['header'], request.raw)
            u.mem_write(HOST+f['fence'], struct.pack('<I', 0xa5a5a5a5))
            if mode == 'resume_marker_corrupt':
                u.mem_write(f['context_buffer']+24, struct.pack('<Q', 2))
            elif mode == 'resume_both':
                u.mem_write(HOST+0x6060, struct.pack('<I', 1))
        status = lib.job_preempt(HOST+f['header'], sequence)
        if mode == 'resume_accept' and cycle == 1:
            assert status == -errno.EBUSY and not lib.job_field(11)
            assert lib.job_field(0) and not lib.job_field(1) and lib.job_field(13)
            assert lib.job_field(9) == 8  # The deferred request consumed no sequence.
            deferrals.append({'status': status, 'firmware_state': word(0x1862a4),
                              'marker': bytes(u.mem_read(f['context_buffer']+24, 8)).hex(),
                              'preempt_sequence': lib.job_field(9)})
            assert lib.job_poll() == -errno.EINPROGRESS  # Firmware consumes resume ACK/marker.
            assert word(0x1862a4) == 4 and state['restore_calls'] == 1
            assert bytes(u.mem_read(f['context_buffer']+24, 8)) == bytes(8)
            status = lib.job_preempt(HOST+f['header'], sequence)
            assert not lib.job_field(13)
        assert not errors, errors
        if mode == 'not_resumable':
            assert status == -errno.EOPNOTSUPP and lib.job_field(0) and not lib.job_field(1)
            assert lib.job_fail() == -errno.ECANCELED
            break
        if mode in ('save_fail', 'disabled', 'timeout', 'late_error', 'drain_read_failure') or (cycle and mode in resume_faults):
            assert status == (-errno.ETIMEDOUT if mode == 'timeout' else
                              -errno.EIO if mode in ('drain_read_failure', 'resume_snapshot_failure') else
                              -errno.ESTALE if mode in ('resume_marker_corrupt', 'resume_both') else -errno.EREMOTEIO)
            assert lib.job_field(1) and not lib.job_field(0) and not lib.job_field(4)
            if mode in resume_faults:
                assert not lib.job_field(11) and lib.job_field(9) == 8
            assert lib.job_resume() == -errno.ENOTRECOVERABLE
            break
        assert status == 0
        if mode.startswith('finished'):
            assert lib.job_field(4) and lib.job_field(5) and not lib.job_field(0)
            assert not lib.job_field(1) and lib.job_field(12) == int(mode == 'finished_before')
            assert lib.job_resume() == -errno.ENOENT
            break
        assert lib.job_field(3) and lib.job_field(0) and not lib.job_field(4)
        before = len(trace)
        assert lib.job_poll() == -errno.EBUSY and lib.job_start() == -errno.EBUSY
        assert lib.job_preempt(HOST+f['header'], sequence+8) == -errno.EBUSY
        assert len(trace) == before
        stages.append({'sequence': sequence, 'polls': lib.job_field(6),
                       'checkpoint': bytes(u.mem_read(f['context_buffer'], 32)).hex()})
        if mode.startswith('stale_') or mode in ('resume_send_failure', 'resume_irq', 'budget', 'abandon'):
            if mode == 'stale_marker':
                u.mem_write(f['context_buffer']+24, bytes(8))
            elif mode == 'stale_sequence':
                u.mem_write(HOST+f['fence'], struct.pack('<I', 7))
            elif mode == 'stale_task':
                u.mem_write(HOST+0x6060, struct.pack('<I', 1))
            elif mode == 'resume_irq':
                u.mem_write(MMIO+8, struct.pack('<I', word(MMIO+8) | 64))
            elif mode == 'budget':
                lib.job_test_polls(5001)
            before = len(trace)
            status = lib.job_fail() if mode == 'abandon' else lib.job_resume()
            assert not errors, errors
            expected = {'resume_send_failure': errno.EIO, 'resume_irq': errno.EPROTO,
                        'budget': errno.ETIMEDOUT, 'abandon': errno.ECANCELED}.get(mode, errno.ESTALE)
            assert status == -expected
            assert lib.job_field(1) and not lib.job_field(0) and not lib.job_field(4)
            if mode != 'resume_send_failure':
                assert not any(t[0] == 0x68 for t in trace[before:])
            assert lib.job_resume() == -errno.ENOTRECOVERABLE
            break
        assert lib.job_resume() == 0 and not lib.job_field(3) and lib.job_field(0)
        assert lib.job_field(6) == stages[-1]['polls'] and not lib.job_field(2)
        # Resume must actually execute both original restore IL records.
        for _ in range(20):
            status = lib.job_poll()
            assert not errors, errors
            stop_for_next = ((mode == 'twice' and word(0x1862a4) == 1) or
                             mode in ('resume_accept', *resume_faults) or
                             (mode in ('restore_first', 'restore_second') and
                              word(0x1862a4) == 4 and state['restore_calls'] ==
                              (1 if mode == 'restore_first' else 2)))
            if repeat and cycle == 0 and lib.job_field(2) and stop_for_next:
                assert status == -errno.EINPROGRESS
                state['events'] = []
                break
            if status != -errno.EINPROGRESS:
                assert status == 0
                break
        else:
            raise AssertionError('resume did not progress')
    assert not errors, errors
    assert state['published'] == 1 and lib.job_field(7) == lib.job_field(8) == 1
    assert bytes(u.mem_read(HOST, 256)) == immutable_task
    assert bytes(u.mem_read(0x100000, 36864)) == firmware[512:512+36864]
    assert bytes(u.mem_read(f['layout'][1]['address'], 3136)) == f['input']
    assert bytes(u.mem_read(f['layout'][2]['address'], 3136)) == b'\xa5'*3136
    if mode in ('save', 'twice', 'idle_after', *restore_modes):
        count = 2 if repeat else 1
        assert lib.job_field(4) and not lib.job_field(5) and not lib.job_field(0)
        assert not lib.job_field(1) and lib.job_field(12)
        restores = (4 if mode == 'restore_second' else 3) if mode in restore_modes else count*2
        assert state['restore_calls'] == restores and executor == [0]*(count+1)
        assert state['save_calls'] == (2 if mode in restore_modes else count*2)
        assert not lib.job_field(13)
        assert bytes(u.mem_read(f['context_buffer']+24, 8)) == bytes(8)
        if mode in restore_modes:
            assert {'irq': '0x4', 'before': 4, 'after': 5} in transitions
            assert {'irq': hex(irq), 'before': 5, 'after': 0} in transitions
    return {'mode': mode, 'engine_irq': hex(irq), 'status': status, 'published': state['published'],
            'stages': stages, 'polls': lib.job_field(6), 'verified_protocol': bool(lib.job_field(4)),
            'poisoned': bool(lib.job_field(1)), 'executor_status': executor,
            'restore_calls': state['restore_calls'], 'method_writes': [t for t in trace if t[0] == 0x68],
            'save_calls': state['save_calls'], 'transitions': transitions, 'deferrals': deferrals,
            'notifications': f['notifications'], 'output_still_poisoned': True}


def main():
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    with tempfile.TemporaryDirectory(prefix='n1x-job-preemption-') as tmp:
        lib = build(Path(tmp))
        cases = [execute(lib, firmware, model, mode, irq)
                 for irq in (0x400, 0x800)
                 for mode in ('save', 'twice', 'save_fail', 'finished', 'finished_before', 'disabled', 'timeout',
                              'late_error', 'idle_after', 'drain_read_failure', 'not_resumable',
                              'stale_marker', 'stale_sequence', 'stale_task', 'resume_irq',
                              'resume_send_failure', 'budget', 'abandon',
                              'resume_accept', 'restore_first', 'restore_second',
                              'resume_marker_corrupt', 'resume_snapshot_failure', 'resume_both')]
    print(json.dumps({'passed': True, 'cases': cases, 'hardware_access': False,
                      'firmware_sha256': PROFILE1_HASH, 'loadable_sha256': hashlib.sha256(model).hexdigest(),
                      'limitations': ['Original firmware with synthetic MMIO/engine IRQs and synchronous RAM DMA',
                                      'Negative fixtures explicitly corrupt DMA markers or inject host transport faults',
                                      'Arithmetic and engine-context transfer are not modeled; no physical resume proof',
                                      'Control component is not wired into public EXEC or linked into bootstrap']}, indent=2))


if __name__ == '__main__':
    main()
