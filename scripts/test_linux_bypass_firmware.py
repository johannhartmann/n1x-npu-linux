#!/usr/bin/env python3
"""Run the exact Linux native task phase against one original HAL-1 firmware CPU."""
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
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as regs
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from test_core_control import IO, READ, WRITE, DELAY

SYNC = ctypes.CFUNCTYPE(None)
STOP = 0x40000000


def compile_native(directory):
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    phase = source[source.index('static int n1x_boot_drain_task_idle('):
                   source.index('static ssize_t bypass_task_store(')]
    shim = r'''
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_task.h"
#include "n1x_dla_command.h"
#include "n1x_dla_response.h"
#include "n1x_dla_job.h"
typedef __u8 u8;typedef __u32 u32;typedef __u64 u64;typedef __u64 dma_addr_t;typedef __u32 __le32;
#define U32_MAX UINT32_MAX
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
#define GFP_KERNEL 0
#define WRITE_ONCE(x,v) ((x)=(v))
#define READ_ONCE(x) (x)
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
struct device {int unused;};struct n1x_boot_endpoint {struct device *dev;};
static struct device device;static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot=&endpoint,*boot_dla=&endpoint;
static struct {bool verified;} boot_ping={true};
static struct {bool registered;} boot_region5={true};
static struct {bool accepted;} boot_config_apply={true},boot_lpwr={true};
static struct n1x_dla_job boot_task;
static struct n1x_dla_job_queue boot_queue;
static bool enable_bypass_task=true;
static unsigned char allocation[4096] __attribute__((aligned(256)));
static struct n1x_dla_io transport;static void (*synchronize)(void);
static bool n1x_boot_machine(void) {return true;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {(void)ep;return 0;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{(void)dev;return mask==DMA_BIT_MASK(36)?0:-EINVAL;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{(void)dev;(void)flags;if(bytes!=sizeof(allocation))return 0;*address=HOST_ADDRESS;return allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{(void)dev;(void)bytes;(void)buffer;(void)address;}
static int n1x_boot_read(void *ctx,u32 offset,u32 *value)
{(void)ctx;return transport.read32(transport.context,offset,value);}
static int n1x_boot_write(void *ctx,u32 offset,u32 value)
{(void)ctx;return transport.write32(transport.context,offset,value);}
static void n1x_boot_delay(void *ctx,u32 delay)
{(void)ctx;transport.delay_us(transport.context,delay);}
static void dma_wmb(void) {}
static void dma_rmb(void) {synchronize();}
'''
    shim = '#define HOST_ADDRESS '+hex(HOST)+'ULL\n'+shim+phase+r'''
void native_queue_reset(void) {memset(&boot_queue,0,sizeof(boot_queue));}
void native_setup(const struct n1x_dla_io *io,void (*sync)(void))
{transport=*io;synchronize=sync;memset(&boot_task,0,sizeof(boot_task));memset(allocation,0,sizeof(allocation));}
void *native_buffer(void) {return allocation;}
int native_run(void) {return n1x_boot_bypass_task_once();}
u32 native_result(unsigned field)
{switch(field){case 0:return boot_task.attempted;case 1:return boot_task.accepted;
 case 2:return boot_task.signal_seen;case 3:return boot_task.verified;case 4:return boot_task.response_valid;
 case 5:return boot_task.response.word;case 6:return boot_task.fence;case 7:return !!boot_task.buffer;
 case 8:return boot_task.fence_value;default:return 0;}}
'''
    code, library = directory/'shim.c', directory/'native.so'
    code.write_text(shim)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
        '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
        str(code), str(ROOT/'driver/n1x_dla_task.c'), str(ROOT/'driver/n1x_dla_command.c'),
        str(ROOT/'driver/n1x_dla_response.c'), str(ROOT/'driver/n1x_dla_job.c'), '-o', str(library)], check=True)
    lib = ctypes.CDLL(str(library))
    lib.native_setup.argtypes = [ctypes.POINTER(IO), SYNC]
    lib.native_buffer.restype = ctypes.c_void_p
    lib.native_result.argtypes = [ctypes.c_uint]
    lib.native_result.restype = ctypes.c_uint32
    return lib


def execute(lib, blob, fault, *, completion_boundary=False, repeat_count=1, revoke_after_acceptance=False):
    lib.native_queue_reset()
    state = {'active': False, 'sent': False, 'paused': False, 'trap_finished': False,
             'scheduler_finished': False, 'device_backing_revoked': False}
    live, frames, errors, trace = [], [], [], []
    completion_snapshots, acceptance_snapshots = [], []

    def word(u, offset):
        return struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]

    def response(u):
        live[:] = [u]
        if not state['active']:
            for target in (8, 0xd28):
                u.mem_write(MMIO+target, struct.pack('<I', word(u, target) & ~128))
            u.mem_write(MMIO+0x40, struct.pack('<I', ACK))
            return
        frames.append((word(u, 0x40), word(u, 0x44)))
        if word(u, 0x40) == 0x702:
            acceptance_snapshots.append(len(initial['dma_commands'])-initial_dma)
        pc = u.reg_read(regs.UC_RISCV_REG_PC)
        size = 4 if int.from_bytes(u.mem_read(pc, 2), 'little') & 3 == 3 else 2
        handle = []

        def pause(_, address, length, data):
            u.hook_del(handle[0])
            state['paused'] = True
            u.emu_stop()

        # Yield only after SWGEN1 store commits; stopping in its memory hook
        # would replay that store when resuming and invalidate host ACK ordering.
        handle.append(u.hook_add(UC_HOOK_CODE, pause, begin=pc+size, end=pc+size))

    # A real original reset/trap/DMA ping initializes the instance and installs
    # the existing exact DMA register model. No firmware function is replaced.
    initial = run(blob, method=0x301, host_payload=bytes(4096), host_handler=response,
                  boot_from_reset=True, via_trap=True, dma_register_model=True)
    assert initial['returned'] and initial['function_stubs'] == []
    u = live[0]
    initial_dma = len(initial['dma_commands'])
    state['active'] = True
    before = []

    def code(_, pc, size, data):
        if pc == 0x102194:
            state['scheduler_finished'] = True
            u.emu_stop()

    def handshake(_, access, address, size, value, data):
        if address == MMIO+0x7c and word(u, 0x7c) & 1:
            u.mem_write(address, struct.pack('<I', word(u, 0x7c) & ~1))

    def signal(_, access, address, size, value, data):
        if address != MMIO or not value & 0x40:
            return
        completion_snapshots.append({
            'pc': hex(u.reg_read(regs.UC_RISCV_REG_PC)),
            'dma_count': len(initial['dma_commands'])-initial_dma,
            'queue_count': struct.unpack('<I', u.mem_read(0x180114, 4))[0],
            'slot_in_use': struct.unpack('<I', u.mem_read(0x18027c, 4))[0],
            'fence': struct.unpack('<I', u.mem_read(HOST+768, 4))[0],
        })
        if completion_boundary:
            pc = u.reg_read(regs.UC_RISCV_REG_PC)
            length = 4 if int.from_bytes(u.mem_read(pc, 2), 'little') & 3 == 3 else 2
            hook = []

            def pause_after_store(_, address, size, user):
                u.hook_del(hook[0])
                state['paused'] = True
                u.emu_stop()

            hook.append(u.hook_add(UC_HOOK_CODE, pause_after_store,
                                   begin=pc+length, end=pc+length))

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_READ, handshake)
    u.hook_add(UC_HOOK_MEM_WRITE, signal)

    def advance():
        if (not state['sent'] or word(u, 8) & 128 or state['scheduler_finished'] or
                (completion_boundary and completion_snapshots)):
            return
        if not before:
            payload = bytearray(ctypes.string_at(lib.native_buffer(), 4096))
            if fault == 'version': payload[8] = 255
            if fault == 'execution': payload[41] = 0
            if fault == 'postaction': struct.pack_into('<H', payload, 62, 13)
            u.mem_write(HOST, bytes(payload))
            method, data = word(u, 0x68), word(u, 0x6c)
            u.mem_write(MMIO+0x64, struct.pack('<I', data))
            u.mem_write(MMIO+8, struct.pack('<I', word(u, 8) | 4))
            before.extend(u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32))
            status = u.reg_read(regs.UC_RISCV_REG_MSTATUS)
            u.reg_write(regs.UC_RISCV_REG_MSTATUS, (status & ~0x1888) | 0x1800 | (0x80 if status & 8 else 0))
            u.reg_write(regs.UC_RISCV_REG_MCAUSE, 0x8000000b)
            u.reg_write(regs.UC_RISCV_REG_MEPC, STOP)
            pc = u.reg_read(regs.UC_RISCV_REG_MTVEC)
        elif state['paused']:
            state['paused'] = False
            pc = u.reg_read(regs.UC_RISCV_REG_PC)
        else:
            pc = 0x102198
        u.emu_start(pc, STOP, count=200000)
        pc = u.reg_read(regs.UC_RISCV_REG_PC)
        if pc == STOP and not state['trap_finished']:
            assert before == [u.reg_read(getattr(regs, f'UC_RISCV_REG_X{i}')) for i in range(1, 32)]
            state['trap_finished'] = True
            u.emu_start(0x102198, STOP, count=200000)
        assert state['paused'] or state['scheduler_finished']

    @READ
    def read(_, offset, out):
        try:
            if offset == 8: advance()
            out[0] = word(u, offset)
            trace.append(('read', offset, out[0]))
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @WRITE
    def write(_, offset, value):
        try:
            trace.append(('write', offset, value))
            if offset in (4, 0xd24):
                target = 8 if offset == 4 else 0xd28
                u.mem_write(MMIO+target, struct.pack('<I', word(u, target) & ~value))
            else:
                u.mem_write(MMIO+offset, struct.pack('<I', value))
            if offset == 0x68: state['sent'] = True
            if (revoke_after_acceptance and offset == 0x40 and value == ACK and
                    frames and frames[-1][0] == 0x702):
                u.mem_unmap(HOST & ~4095, ((HOST & 4095)+4096+4095) & ~4095)
                state['device_backing_revoked'] = True
            return 0
        except Exception as exc:
            errors.append(repr(exc))
            return -errno.EIO

    @DELAY
    def delay(_, interval):
        pass

    @SYNC
    def sync():
        if not state['sent'] or state['device_backing_revoked']:
            return  # CPU backing is retained; only modeled device mapping is revoked.
        try:
            ctypes.memmove(lib.native_buffer(), bytes(u.mem_read(HOST, 4096)), 4096)
        except Exception as exc:
            errors.append(repr(exc))

    io = IO(None, write, read, delay)
    assert 1 <= repeat_count <= 64 and (repeat_count == 1 or fault is None)
    runs = []
    for iteration in range(repeat_count):
        if iteration:
            # Reset only the native harness's one-shot state. The original
            # firmware CPU, globals, queue and allocations are not reset.
            assert state['scheduler_finished'] and not word(u, 8) & 0xc0
            state.update(sent=False, paused=False, trap_finished=False,
                         scheduler_finished=False)
            before.clear()
            frames.clear()
            completion_snapshots.clear()
            acceptance_snapshots.clear()
            initial_dma = len(initial['dma_commands'])
        lib.native_setup(ctypes.byref(io), sync)
        status = lib.native_run()
        if not revoke_after_acceptance:
            assert not errors, errors
        fields = [lib.native_result(i) for i in range(9)]
        assert fields[0] == fields[7] == 1 and lib.native_run() == -errno.EALREADY
        assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
        if revoke_after_acceptance:
            assert repeat_count == 1 and not completion_boundary and fault is None
            assert status == -errno.EIO and len(errors) == 1, errors
            assert state['device_backing_revoked']
            assert fields[:4] == [1, 1, 0, 0] and fields[7] == 1
            assert acceptance_snapshots == [4] and not completion_snapshots
            assert not state['scheduler_finished']
            return {'negative_control': 'revoke task backing after acceptance, before completion',
                    'native_return': status, 'dma_access_rejected': errors[0],
                    'accepted': True, 'verified': False, 'dma_retained_by_native': True}
        native_return_pc = hex(u.reg_read(regs.UC_RISCV_REG_PC))
        scheduler_finished_on_native_return = state['scheduler_finished']
        if completion_boundary:
            assert fault is None and not state['scheduler_finished'] and state['paused']
            assert completion_snapshots and completion_snapshots[-1]['dma_count'] == 11
            assert completion_snapshots[-1]['queue_count'] == 0
            assert completion_snapshots[-1]['slot_in_use'] == 0
            assert acceptance_snapshots == [4]
            # Revoking completed host memory is stronger than only watching the
            # already-idle firmware. Any tail DMA access now fails in the model.
            original_backing = bytes(u.mem_read(HOST, 4096))
            u.mem_unmap(HOST & ~4095, ((HOST & 4095)+4096+4095) & ~4095)
            u.emu_start(u.reg_read(regs.UC_RISCV_REG_PC), STOP, count=200000)
            u.mem_map(HOST & ~4095, ((HOST & 4095)+4096+4095) & ~4095)
            u.mem_write(HOST, original_backing)
            assert state['scheduler_finished']
            assert len(initial['dma_commands'])-initial_dma == 11
        if fault is None:
            assert status == 0 and fields[1:5] == [1, 1, 1, 1]
            assert fields[5:7] == [0x702, iteration+1] and fields[8] == iteration+1
            assert frames == [(0x702, HOST >> 8)] and word(u, 8) == 0
            assert state['trap_finished'] and state['scheduler_finished']
            assert struct.unpack('<I', u.mem_read(0x180114, 4))[0] == 0
        else:
            assert status == -errno.EREMOTEIO and not fields[3]
            expected = {'version': 0x90701, 'execution': 0x8003070a, 'postaction': 0x8007070a}[fault]
            assert fields[5] == expected and frames[-1] == (expected, HOST >> 8)
            assert fields[6] == (fields[8] ^ 0xffffffff)
            # Native stops immediately on error, so its last CPU fence observation
            # can predate the firmware's postaction write. Inspect modeled DMA RAM.
            actual_fence = struct.unpack('<I', u.mem_read(HOST+768, 4))[0]
            assert actual_fence == (fields[8] if fault == 'postaction' else fields[8] ^ 0xffffffff)
        assert len(initial['dma_commands'])-initial_dma == {None: 11, 'version': 4, 'execution': 13, 'postaction': 11}[fault]
        record = {'fault': fault, 'native_return': status, 'frames': list(frames),
                'completion_value': fields[8], 'accepted': bool(fields[1]), 'verified': bool(fields[3]),
                'dma_retained': bool(fields[7]), 'firmware_code_unmodified': True,
                'original_dma_commands': len(initial['dma_commands'])-initial_dma,
                'completion_boundary': completion_boundary, 'native_return_pc': native_return_pc,
                'scheduler_finished_on_native_return': scheduler_finished_on_native_return,
                'completion_snapshots': list(completion_snapshots),
                'acceptance_dma_counts': list(acceptance_snapshots),
                'backing_revoked_during_firmware_tail': completion_boundary,
                'trap_finished': state['trap_finished'], 'scheduler_finished': state['scheduler_finished']}
        runs.append(record)
    return {'repeat_count': repeat_count, 'firmware_reset_count': 1, 'runs': runs}



def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-native-fw-') as tmp:
        lib = compile_native(Path(tmp))
        rows = [execute(lib, blob, fault) for fault in (None, 'version', 'execution', 'postaction')]
        rows.append(execute(lib, blob, None, completion_boundary=True, repeat_count=32))
        rows.append(execute(lib, blob, None, revoke_after_acceptance=True))
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'limitations': ['Kernel machine/DMA/allocation/prior-phase gates are synthetic',
                                      'Original firmware reset/trap/scheduler/DMA code runs; no firmware function stubs',
                                      'MMIO/FIFO/IRQ, synchronous DMA completion, coherence and handshake devices are modeled',
                                      'Error cases stop with retained backing while firmware may still be in its IRQ handler',
                                      'Repeated runs preserve the native queue and firmware; only per-job host state is reset; sysfs probes remain one-shot',
                                      'Post-SWGEN0 revocation covers bypass descriptors, not neural-engine buffers or teardown',
                                      'No opaque-image equivalence, physical boot/IRQ/DMA or numerical inference']}, indent=2))


if __name__ == '__main__':
    main()
