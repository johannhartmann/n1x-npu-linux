#!/usr/bin/env python3
"""Qualify the original HAL-1 scheduler success/error paths without computation."""
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
from emulate_profile1_commands import run, ROOT, HOST, MMIO, PROFILE1_HASH
from test_core_control import IO, READ, WRITE, DELAY
from test_response import Response
from test_task_descriptor import Args


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    header = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/dla_os_interface.h'
    assert hashlib.sha256(header.read_bytes()).hexdigest() == 'e962bcaae697269981083196eed90a495949c636925d43ea4c398337de7c6162'
    with tempfile.TemporaryDirectory(prefix='n1x-bypass-') as tmp:
        library = Path(tmp)/'task.so'
        reference = Path(tmp)/'reference.c'
        reference.write_text('''#include <stdint.h>
#include <stddef.h>
#define __packed __attribute__((packed))
#define __aligned(n) __attribute__((aligned(n)))
#include "dla_os_interface.h"
_Static_assert(offsetof(struct dla_task_descriptor, flags)==41,"flags offset");
_Static_assert(DLA_DESC_FLAGS_BYPASS_EXEC==1,"bypass bit");
''')
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), '-I'+str(header.parent),
            str(ROOT/'driver/n1x_dla_task.c'), str(ROOT/'driver/n1x_dla_command.c'),
            str(ROOT/'driver/n1x_dla_response.c'), str(reference), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    build = lib.n1x_dla_build_bypass_fenced_task_v2
    fenced = lib.n1x_dla_build_fenced_task_v2
    for function in (build, fenced):
        function.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Args),
                             ctypes.c_uint64, ctypes.c_uint32]
    send = lib.n1x_dla_send_method
    send.argtypes = [ctypes.POINTER(IO)]+[ctypes.c_uint32]*4
    take = lib.n1x_dla_take_response
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Response)]
    args = Args(0, HOST+256, 1000000, 1)
    invalid = 0
    for size, pointer, fence in ((255, ctypes.byref(args), HOST+768),
                                (256, None, HOST+768), (256, ctypes.byref(args), 0),
                                (256, ctypes.byref(args), HOST+769)):
        output = ctypes.create_string_buffer(b'Z'*260, 260)
        assert build(output, size, pointer, fence, 1) == -errno.EINVAL
        assert output.raw == b'Z'*260
        invalid += 1
    chained = Args(HOST, HOST+256, 0, 1)
    output = ctypes.create_string_buffer(b'Z'*260, 260)
    assert build(output, 256, ctypes.byref(chained), HOST+768, 1) == -errno.EINVAL
    assert output.raw == b'Z'*260
    assert build(None, 256, ctypes.byref(args), HOST+768, 1) == -errno.EINVAL
    invalid += 2
    rows = []
    cases = [(f'success-{value:x}', value, None) for value in (0, 1, 0x12345678, 0xffffffff)]
    cases += [('execution-error', 0x12345678, 'no-bypass'),
              ('postaction-error-after-fence-write', 0x12345678, 'bad-list'),
              ('descriptor-rejected', 0x12345678, 'bad-version')]
    for name, value, change in cases:
        task, plain = ctypes.create_string_buffer(b'Z'*260, 260), ctypes.create_string_buffer(256)
        assert build(task, 260, ctypes.byref(args), HOST+768, value) == 0
        assert fenced(plain, 256, ctypes.byref(args), HOST+768, value) == 0
        expected = bytearray(plain.raw)
        expected[41] = 1
        assert task.raw == expected+b'Z'*4
        payload = bytearray(1024)
        payload[:256] = task.raw[:256]
        struct.pack_into('<Q', payload, 256, HOST+512)  # Deliberately invalid zero network.
        initial = value ^ 0xffffffff
        struct.pack_into('<I', payload, 768, initial)
        if change == 'no-bypass': payload[41] = 0
        if change == 'bad-list': struct.pack_into('<H', payload, 62, 13)
        if change == 'bad-version': payload[8] = 255
        captured, submit_trace, ack_traces = [], [], []

        def io_for(u, trace):
            @READ
            def read(_, offset, out):
                out[0] = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
                trace.append(('read', offset, out[0]))
                return 0

            @WRITE
            def write(_, offset, word):
                trace.append(('write', offset, word))
                if offset in (4, 0xd24):
                    target = 8 if offset == 4 else 0xd28
                    old = struct.unpack('<I', u.mem_read(MMIO+target, 4))[0]
                    u.mem_write(MMIO+target, struct.pack('<I', old & ~word))
                else:
                    u.mem_write(MMIO+offset, struct.pack('<I', word))
                return 0

            # Keep callback objects alive for the synchronous C call.
            return IO(None, write, read, DELAY()), (read, write)

        def submit(u):
            io, callbacks = io_for(u, submit_trace)
            assert send(ctypes.byref(io), 0x307, HOST >> 8, 1, 0) == 0

        def response(u):
            trace = []
            io, callbacks = io_for(u, trace)
            result = Response()
            assert take(ctypes.byref(io), ctypes.byref(result)) == 0
            captured.append((result.word, result.data))
            ack_traces.append(trace)

        result = run(blob, host_payload=payload, host_submit=submit, host_handler=response,
                     boot_from_reset=True, via_trap=True, dma_register_model=True,
                     resume_scheduler=True)
        assert result['returned'] and result['scheduler_wfi']
        assert result['firmware_code_unmodified'] and result['function_stubs'] == []
        assert result['trap_returns'] == [0x100324]
        assert result['interrupts'] == captured
        assert len(submit_trace) == 3 and len(ack_traces) == len(captured)
        final = bytes.fromhex(result['host_payload_final'])
        fence_value = struct.unpack_from('<I', final, 768)[0]
        assert struct.unpack_from('<I', bytes.fromhex(result['queue0']), 20)[0] == 0
        slot = bytes.fromhex(result['first_task_slot'])
        assert struct.unpack_from('<I', slot, 124)[0] == 0
        if change is None:
            assert captured == [(0x702, HOST >> 8)]
            assert fence_value == value
            assert result['task_interrupts'] == [{'pending': 64, 'mask': 0x1f45}]
            assert result['final_irq_pending'] == 64
            assert len(result['dma_commands']) == 11
            # First 8 reads are the two 60-byte descriptor fetches, then list
            # table, padded descriptor cache fetch and the terminal fence write.
            assert [item['bytes'] for item in result['dma_commands']] == [32, 16, 8, 4]*2+[4, 256, 4]
            assert result['dma_commands'][-1]['destination'] == HOST+768
            assert all(item['source'] < HOST+256 for item in result['dma_commands'] if item['direction']=='read')
            assert result['task_events'] == ['0x104766', '0x1014f6', '0x103d56', '0x103f9c']
            assert not result['scheduler_engine_reads']
        elif change == 'no-bypass':
            assert captured == [(0x702, HOST >> 8), (0x8003070a, HOST >> 8)]
            assert fence_value == initial and not result['task_interrupts']
        elif change == 'bad-list':
            assert captured == [(0x702, HOST >> 8), (0x8007070a, HOST >> 8)]
            assert fence_value == value and not result['task_interrupts']
        else:
            assert captured == [(0x90701, HOST >> 8)]
            assert fence_value == initial and not result['task_interrupts']
        # Observe original signal ordering; reading final RAM alone would not
        # show that the fence precedes SWGEN0. No host SWGEN0 ACK is synthesized.
        if change is None:
            writes = result['scheduler_mmio_writes']
            signal = next(i for i, (offset, _, word) in enumerate(writes) if offset == 0 and word == 64)
            last_dma = max(i for i, (offset, _, _) in enumerate(writes) if offset == 0x118)
            assert last_dma < signal
        rows.append({'case': name, 'mailbox_frames': captured, 'fence': hex(fence_value),
                     'swgen0': result['task_interrupts'], 'dma_commands': len(result['dma_commands']),
                     'queue_empty': True, 'firmware_code_unmodified': True})
    # Parameters may overlap output; serialization must copy before writing.
    alias = ctypes.create_string_buffer(256)
    ctypes.memmove(alias, ctypes.byref(args), ctypes.sizeof(args))
    assert build(alias, 256, ctypes.cast(alias, ctypes.POINTER(Args)), HOST+768, 1) == 0
    assert alias.raw[41:43] == b'\x01\x00'
    print(json.dumps({'passed': True, 'hardware_access': False, 'firmware_sha256': PROFILE1_HASH,
                      'preflight_failures': invalid, 'overlapping_args_checked': True, 'cases': rows,
                      'limitations': ['Reset, method FIFO, DMA, IRQ and handshake devices are synthetic',
                                      'BYPASS_EXEC skips network/instruction execution, not all control-register accesses',
                                      'No physical completion, complete DMA quiescence, opaque image equivalence or numerical inference',
                                      'Malformed postactions can write a fence and subsequently report failure']}, indent=2))


if __name__ == '__main__':
    main()
