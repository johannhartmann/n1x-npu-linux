#!/usr/bin/env python3
"""Compare the C command-23 record with original Windows construction and RV32 import."""
import ctypes
import errno
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_profile1_commands import run as run_firmware, ROOT, HOST, MMIO, ACK, PROFILE1_HASH

BASE = 0x140000000
CTX, OUTER, DEVICE, PROVIDER, BUFFER, STACK, STUB = (0x2c000000+i*0x10000 for i in range(7))
STOP = STUB+0xff0


def windows(pe, flag, scale, provider, allocation_error=0, null_cpu=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva in (0xd45000, 0xd57000, 0xd39000, 0xd3b000, 0xcd000, 0xcc000, 0x1512000):
        u.mem_map(BASE+rva, 0x1000)
        u.mem_write(BASE+rva, pe.get_data(rva, 0x1000))
    for address in (CTX, OUTER, DEVICE, PROVIDER, BUFFER, STACK, STUB):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160, 0xd3b0f8, 0xcd148, 0xccee8, 0xd3b298):
        u.mem_write(BASE+rva, ret)
    u.mem_write(BASE+0x1512148, struct.pack('<Q', DEVICE))
    u.mem_write(DEVICE+0x78, struct.pack('<Q', PROVIDER))
    u.mem_write(PROVIDER+0xf4, struct.pack('<I', provider))
    u.mem_write(CTX+0x47d, bytes([flag]))
    u.mem_write(CTX+0x490, struct.pack('<I', scale))
    u.mem_write(CTX+0x4e8, b'\x5a'*16)
    u.mem_write(BUFFER, b'\xa5'*4096)
    u.mem_write(STACK, b'\xa5'*0x10000)
    events = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            assert reg(0) == DEVICE
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        elif address == BASE+0xd3b0f8:
            assert reg(0) == OUTER and reg(1) == OUTER and reg(2) == 4096
            if not allocation_error:
                u.mem_write(reg(3), struct.pack('<Q', 0 if null_cpu else BUFFER))
                u.mem_write(reg(4), struct.pack('<Q', HOST))
            events.append('allocate')
            u.reg_write(regs.UC_ARM64_REG_X0, allocation_error)
        elif address == BASE+0xcd148:
            assert reg(1) == 0 and reg(2) in (40, 4096)
            u.mem_write(reg(0), bytes(reg(2)))
        elif address == BASE+0xccee8:
            assert reg(0) == BUFFER and reg(1) == 4096 and reg(3) == 40
            u.mem_write(reg(0), bytes(u.mem_read(reg(2), 40)))
            events.append('copy40')
        elif address == BASE+0xd3b298:
            assert [reg(i) for i in range(4)] == [OUTER, BUFFER, 4096, 1]
            events.append('cache_boundary')

    install_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for r, value in ((regs.UC_ARM64_REG_X0, OUTER), (regs.UC_ARM64_REG_X1, CTX),
                     (regs.UC_ARM64_REG_SP, STACK+0x8000), (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(r, value)
    u.emu_start(BASE+0xd45390, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': u.reg_read(regs.UC_ARM64_REG_X0), 'events': events,
            'record': bytes(u.mem_read(BUFFER, 40)),
            'tail': bytes(u.mem_read(BUFFER+40, 4096-40)),
            'context': bytes(u.mem_read(CTX+0x4e8, 16))}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    with tempfile.TemporaryDirectory(prefix='n1x-command23-') as tmp:
        library = Path(tmp)/'command.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_command.c'), '-o', str(library)], check=True)
        # Compile the actual GPL L4T layout as an independent structure check.
        # Matching bytes/IDs do not prove matching field units on this P16.
        header = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/dla_os_interface.h'
        assert hashlib.sha256(header.read_bytes()).hexdigest() == 'e962bcaae697269981083196eed90a495949c636925d43ea4c398337de7c6162'
        text = header.read_text()
        assert re.search(r'DLA_CMD_SET_LPWR_CONFIG\s*=\s*23U', text)
        assert re.search(r'DLA_CMD_GET_LPWR_CONFIG\s*=\s*24U', text)
        structure = re.search(r'struct dla_lpwr_config \{.*?\} __packed __aligned\(4\);', text, re.S)[0]
        layout = Path(tmp)/'layout.c'
        layout.write_text('#include <stdint.h>\n#include <stddef.h>\n'
                          '#define __packed __attribute__((packed))\n'
                          '#define __aligned(n) __attribute__((aligned(n)))\n'+structure+
                          '\n_Static_assert(sizeof(struct dla_lpwr_config)==40,"size");\n'
                          '_Static_assert(offsetof(struct dla_lpwr_config, version)==0,"version");\n'
                          '_Static_assert(offsetof(struct dla_lpwr_config, idle_notification_enable)==1,"flag");\n'
                          '_Static_assert(offsetof(struct dla_lpwr_config, idledelay_us)==2,"delay");\n'
                          '_Static_assert(offsetof(struct dla_lpwr_config, reserved)==6,"tail");\n'
                          '_Static_assert(DLA_IDLE_NOTIFICATION_DISABLE==2,"different disable encoding");\n')
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+['-Wall', '-Wextra', '-Werror',
                        '-c', str(layout), '-o', str(Path(tmp)/'layout.o')], check=True)
        lib = ctypes.CDLL(str(library))
        build = lib.n1x_dla_build_command23
        build.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32]
        for flag in (0, 1, 255):
            for scale in (0, 1, 0xffffffff):
                for provider in (0, 1, 19200, 0xffffffff//2000, 0xffffffff):
                    win = windows(pe, flag, scale, provider)
                    output = ctypes.create_string_buffer(b'Z'*44, 44)
                    assert build(output, 44, flag, scale, provider) == 0
                    record = output.raw[:40]
                    assert record == win['record'] and output.raw[40:] == b'Z'*4
                    assert win['status'] == 0 and win['tail'] == bytes(4096-40)
                    assert win['events'] == ['allocate', 'copy40', 'cache_boundary']
                    assert win['context'] == struct.pack('<QQ', BUFFER, HOST)
                    rows.append({'flag': flag, 'scale': scale, 'provider': provider, 'record': record.hex()})
        output = ctypes.create_string_buffer(b'Z'*40, 40)
        assert build(output, 39, 1, 1, 1) == -errno.EINVAL and output.raw == b'Z'*40
        assert build(None, 40, 1, 1, 1) == -errno.EINVAL
        for kwargs, status in (({'allocation_error': 0x65}, 0x65), ({'null_cpu': True}, 0x1a)):
            win = windows(pe, 1, 1, 19200, **kwargs)
            assert win['status'] == status and win['events'] == ['allocate']
            assert win['context'] == b'\x5a'*16 and win['record'] == b'\xa5'*40
        record = bytes.fromhex(rows[-1]['record'])
        observed = []
        def response(u):
            observed.append(bytes(u.mem_read(0x186254, 40)))
            u.mem_write(MMIO+0x40, struct.pack('<I', ACK))
        result = run_firmware(blob, method=0x317, boot_from_reset=True, via_trap=True,
                              dma_register_model=True, host_payload=record+bytes(216), host_handler=response)
        assert result['returned'] and result['firmware_code_unmodified'] and result['function_stubs'] == []
        assert result['interrupts'] == [(0x1702, HOST >> 8)] and observed == [record]
    print(json.dumps({'passed': True, 'hardware_access': False, 'differential_cases': len(rows),
                      'cases': rows, 'windows_allocation_failure_cases': 2, 'linux_preflight_cases': 2,
                      'l4t_lpwr_layout_and_command_ids_match': True,
                      'zero_flag_comparison': {'windows_builder_if_called_with_zero': 0, 'l4t_disable_enum': 2},
                      'limitations': ['OS allocation/copy/cache boundaries are synthetic',
                                      'Provider values are test inputs, not measured physical clock/timer values',
                                      'Raw HAL-1 firmware import runs unchanged under modeled DMA/reset/IRQ',
                                      'No live command23 submission, firmware boot or inference is established']}, indent=2))


if __name__ == '__main__':
    main()
