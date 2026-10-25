#!/usr/bin/env python3
"""Compare C defaults with original Windows modifier and same-instance HAL-1 roundtrip."""
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
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH

BASE = 0x140000000
CTX, BUFFER, OVERRIDES, STACK, STOP = (0x39000000+i*0x10000 for i in range(5))


def windows(pe, payload, *, override_table=False, cpu=True, dma=HOST):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd47000, 0xd48000, 0xd39000, 0x1512000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, BUFFER, OVERRIDES, STACK, STOP):
        u.mem_map(address, 0x10000)
    u.mem_write(BUFFER, payload)
    u.mem_write(CTX+0x4f8, struct.pack('<QQ', BUFFER if cpu else 0, dma))
    u.mem_write(CTX+0x640, struct.pack('<Q', OVERRIDES if override_table else 0))
    u.mem_write(CTX+0x47e, b'\x01')
    u.mem_write(CTX+0x480, struct.pack('<I', 1))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
            u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd47830, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return u.reg_read(regs.UC_ARM64_REG_X0), bytes(u.mem_read(BUFFER, len(payload)))


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-config-defaults-') as tmp:
        library = Path(tmp)/'command.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    prepare = lib.n1x_dla_prepare_default_config
    prepare.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    cases = 0
    for payload in (bytes(4096), b'\xa5'*4096, bytes(i & 255 for i in range(4096))):
        for table in (False, True):
            status, expected = windows(pe, payload, override_table=table)
            assert status == 0
            output = ctypes.create_string_buffer(payload, len(payload))
            assert prepare(output, len(payload)) == 0 and output.raw == expected
            assert [i for i, (a, b) in enumerate(zip(payload, expected)) if a != b] == [1, 50]
            cases += 1
    original = b'\xa5'*4096
    for args in ({'cpu': False}, {'dma': 0}):
        status, unchanged = windows(pe, original, **args)
        assert status == 0x40 and unchanged == original
    output = ctypes.create_string_buffer(original, len(original))
    assert prepare(output, 251) == -errno.EINVAL and output.raw == original
    assert prepare(None, 252) == -errno.EINVAL
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    payload = bytearray(4096)
    steps = iter((22, 21, 22))
    local, prepared, exported = [], [], []

    def submit(u):
        command = next(steps)
        u.mem_write(MMIO+0x68, struct.pack('<II', 0x300 | command, HOST >> 8))

    def response(u):
        step = len(local)
        local.append(bytes(u.mem_read(0x186154, 252)))
        if step == 0:
            actual_export = bytes(u.mem_read(HOST, 4096))
            assert actual_export[:252] == local[0]
            status, expected = windows(pe, actual_export)
            assert status == 0
            output = ctypes.create_string_buffer(actual_export, len(actual_export))
            assert prepare(output, len(actual_export)) == 0 and output.raw == expected
            prepared.append(expected)
            payload[:] = expected  # The next same-instance command imports it.
        elif step == 1:
            assert local[1] == prepared[0][:252]
            payload[:] = bytes(4096)  # Erase host contents before re-export.
        else:
            exported.append(bytes(u.mem_read(HOST, 4096)))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, sequence=(0, 0, 0), host_payload=payload, host_submit=submit,
                 host_handler=response, boot_from_reset=True, via_trap=True,
                 dma_register_model=True)
    assert result['returned'] and result['firmware_code_unmodified'] and result['function_stubs'] == []
    assert result['interrupts'] == [(0x1602, HOST >> 8), (0x1502, HOST >> 8), (0x1602, HOST >> 8)]
    assert len(result['trap_returns']) == 3 and len(result['dma_commands']) == 189
    assert local[0][0] == 2 and local[1] == local[2] == exported[0][:252]
    assert exported[0][:252] == prepared[0][:252] and exported[0][252:] == bytes(4096-252)
    changed = [i for i, (a, b) in enumerate(zip(local[0], local[1])) if a != b]
    assert changed == [1, 50]
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_modifier': '0xd47830', 'windows_c_differential_cases': cases,
                      'same_firmware_instance_commands': [22, 21, 22], 'changed_offsets': changed,
                      'firmware_code_unmodified': True, 'firmware_function_stubs': [],
                      'limitations': ['OS getter, Windows payload and DMA/IRQ/reset effects are synthetic',
                                      'Only absent or all-disabled override tables; effective custom overrides are not ported',
                                      'Changes are observed defaults, not inferred physical meanings/units',
                                      'No physical import, opaque firmware identity, full initialization or inference']}, indent=2))


if __name__ == '__main__':
    main()
