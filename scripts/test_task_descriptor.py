#!/usr/bin/env python3
"""Compare Linux serialization with the public packed struct and actual FW checks."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from emulate_task_header_checks import run, BLOB_HASH
from emulate_profile1_task_header import check as check_profile1
from extract_profile1_firmware import PROFILE1_HASH
from emulate_postactions import run as run_postactions, HOST
from emulate_profile1_postactions import run as run_profile1_postactions

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/dla_os_interface.h'


class Args(ctypes.Structure):
    _fields_ = [('next', ctypes.c_uint64), ('address_list', ctypes.c_uint64),
                ('timeout', ctypes.c_uint64), ('num_addresses', ctypes.c_uint16)]


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    profile1 = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(profile1).hexdigest() == PROFILE1_HASH
    profile1_code = profile1[512:512+36864]
    reference_hash = hashlib.sha256(REFERENCE.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='n1x-dla-task-') as tmp:
        library = Path(tmp)/'task.so'
        shim = Path(tmp)/'reference.c'
        shim.write_text('''#include <stdint.h>
#include <stddef.h>
#include <string.h>
#define __packed __attribute__((packed))
#define __aligned(x) __attribute__((aligned(x)))
#include "dla_os_interface.h"
_Static_assert(sizeof(struct dla_task_descriptor) == 60, "reference descriptor size");
void reference(uint8_t *out, uint64_t next, uint64_t addresses,
               uint16_t count, uint64_t timeout) {
    struct dla_task_descriptor task;
    memset(&task, 0, sizeof(task));
    task.next = next;
    task.version = DLA_DESCRIPTOR_VERSION;
    task.engine_id = DLA_ENGINE_ID;
    task.size = sizeof(task);
    task.address_list = addresses;
    task.num_addresses = count;
    task.timeout = timeout;
    memcpy(out, &task, sizeof(task));
}
void reference_fenced(uint8_t *out, uint64_t next, uint64_t addresses,
                      uint16_t count, uint64_t timeout, uint64_t fence, uint32_t value) {
    struct dla_task_descriptor task;
    struct dla_action_list list = {64, 14};
    struct dla_action_semaphore sem = {fence, value};
    reference((uint8_t *)&task, next, addresses, count, timeout);
    task.size = 96;
    task.num_postactions = 1;
    task.postactions = 60;
    memset(out, 0, 256);
    memcpy(out, &task, sizeof(task));
    memcpy(out + 60, &list, sizeof(list));
    out[64] = 0x80;
    memcpy(out + 65, &sem, sizeof(sem));
}
''')
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), '-I'+str(REFERENCE.parent),
            str(ROOT/'driver/n1x_dla_task.c'), str(shim), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    build = lib.n1x_dla_build_task_v2
    build.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Args)]
    reference = lib.reference
    reference.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64,
                          ctypes.c_uint16, ctypes.c_uint64]
    for params in (Args(0, 0x10000, 0, 9), Args(0x1234567800, 0xabcdef00, 0x123456789abcdef0, 65535)):
        output = ctypes.create_string_buffer(b'\xa5'*64, 64)
        expected = ctypes.create_string_buffer(60)
        assert build(output, 64, ctypes.byref(params)) == 0
        reference(expected, params.next, params.address_list, params.num_addresses, params.timeout)
        assert output.raw[:60] == expected.raw and output.raw[60:] == b'\xa5'*4
        assert run(blob[0x200:0xb100], raw_header=output.raw[:60]) == 'accepted_common_header'
        assert check_profile1(profile1_code, raw_header=output.raw[:60]) == 'accepted_common_header'
    for params in (Args(1, 0x10000, 0, 9), Args(0, 0, 0, 9),
                   Args(0, 0x10001, 0, 9), Args(0, 0x10008, 0, 9), Args(0, 0x10000, 0, 0)):
        output = ctypes.create_string_buffer(b'\xa5'*64, 64)
        assert build(output, 64, ctypes.byref(params)) == -errno.EINVAL
        assert output.raw == b'\xa5'*64
    output = ctypes.create_string_buffer(b'\xa5'*64, 64)
    params = Args(0, 0x10000, 0, 9)
    assert build(output, 59, ctypes.byref(params)) == -errno.EINVAL and output.raw == b'\xa5'*64
    assert build(None, 60, ctypes.byref(params)) == -errno.EINVAL
    assert build(output, 64, None) == -errno.EINVAL and output.raw == b'\xa5'*64
    fenced = lib.n1x_dla_build_fenced_task_v2
    fenced.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Args),
                        ctypes.c_uint64, ctypes.c_uint32]
    ref_fenced = lib.reference_fenced
    ref_fenced.argtypes = reference.argtypes + [ctypes.c_uint64, ctypes.c_uint32]
    for value in (0, 1, 0x12345678, 0xffffffff):
        output = ctypes.create_string_buffer(b'\xa5'*260, 260)
        expected = ctypes.create_string_buffer(256)
        assert fenced(output, 260, ctypes.byref(params), HOST, value) == 0
        ref_fenced(expected, params.next, params.address_list, params.num_addresses,
                   params.timeout, HOST, value)
        assert output.raw[:256] == expected.raw and output.raw[256:] == b'\xa5'*4
        assert run(blob[0x200:0xb100], raw_header=output.raw[:60]) == 'accepted_common_header'
        assert check_profile1(profile1_code, raw_header=output.raw[:60]) == 'accepted_common_header'
        result = run_postactions(blob, b'', descriptor=output.raw[:256])
        assert result['returned'] and result['status'] == 0 and result['host_word'] == value
        assert [t['direction'] for t in result['transfers']] == ['read', 'read', 'read', 'write']
        target = run_profile1_postactions(profile1, b'', descriptor=output.raw[:256])
        assert target['returned'] and target['status'] == 0 and target['host_word'] == value
        assert [t['length'] for t in target['transfers']] == [4, 256, 4]
        short = run_profile1_postactions(profile1, b'', descriptor=output.raw[:96])
        assert short['returned'] and short['status'] == -8 and short['host_word'] == 0xdeadbeef
    for task_status in (1, -4):
        skipped = run_profile1_postactions(profile1, b'', descriptor=output.raw[:256], task_status=task_status)
        assert skipped['returned'] and skipped['status'] == 0
        assert not skipped['transfers'] and skipped['host_word'] == 0xdeadbeef
    for list_size in (13, 15):
        malformed = bytearray(output.raw[:256])
        malformed[62:64] = list_size.to_bytes(2, 'little')
        failed = run_profile1_postactions(profile1, b'', descriptor=malformed)
        assert failed['returned'] and failed['status'] == -7 and failed['host_word'] == 0xffffffff
    for size, address, arguments in ((96, HOST, ctypes.byref(params)),
                                      (255, HOST, ctypes.byref(params)),
                                      (256, 0, ctypes.byref(params)),
                                      (256, HOST+1, ctypes.byref(params)),
                                      (256, HOST, None)):
        output = ctypes.create_string_buffer(b'\xa5'*260, 260)
        assert fenced(output, size, arguments, address, 1) == -errno.EINVAL
        assert output.raw == b'\xa5'*260
    assert fenced(None, 256, ctypes.byref(params), HOST, 1) == -errno.EINVAL
    print(json.dumps({'scope': 'C serialization versus compiled public L4T struct and RISC-V common-header checks',
                      'reference_header_sha256': reference_hash,
                      'task_bytes': 60, 'profile1_common_header_serializer_cases': 6, 'fenced_task_allocation_bytes': 256, 'fenced_task_logical_bytes': 96,
                      'profile1_postaction_serializer_cases': 4, 'profile1_undersized_backing_rejections': 4,
                      'profile1_failed_task_skips_postactions': 2, 'profile1_malformed_fenced_lists': 2,
                      'fenced_reference_header_and_firmware_list_cases': 4, 'valid_reference_and_firmware_cases': 2,
                      'basic_preflight_failures': 8, 'fenced_preflight_failures': 6,
                      'limitations': ['Full descriptor/address/action semantics have not been proven on the NPU',
                                      'Only terminal WRITE_SEM lists are checked; other HAL-1 actions remain unverified',
                                      'DMA buffers, lifetime, firmware completion and numerical inference remain open'],
                      'hardware_access': False, 'passed': True}, indent=2))


if __name__ == '__main__':
    main()
