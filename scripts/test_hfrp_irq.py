#!/usr/bin/env python3
"""Cross-check original Windows HFRP IRQ, parent dispatch, L4T header and C port."""
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
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from test_core_control import IO, READ, WRITE, DELAY

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, LINK, OWNER, PARENT, ROOTCTX, PRIMARY, OUTER, STACK, STUB = (0x34000000+i*0x10000 for i in range(9))
STOP = STUB+0xff0
HEADER_HASH = 'ca7bc4d365772cedcc09ae08b43cd2f232a581b14c748d9b8642e33b2e3f6e89'


class Result(ctypes.Structure):
    _fields_ = [('status', ctypes.c_uint32), ('status_valid', ctypes.c_uint8),
                ('ack_attempted', ctypes.c_uint8), ('acknowledged', ctypes.c_uint8)]


def windows(pe, status, *, mode=0, counter=0, pending=0, read_error=0,
            write_error=0, read_output=True, parent=False, primary_status=0,
            enabled=True, primary_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd38000, 0xd39000, 0xd3d000, 0xd4a000, 0xd4b000,
                 0xd57000, 0xcd000, 0xe3e000, 0x238000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, LINK, OWNER, PARENT, ROOTCTX, PRIMARY, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6')*16)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    trace, primary_calls, schedules = [], [], []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xcd148:
            u.mem_write(reg(0), bytes(reg(2)))
            ret(reg(0))
        elif address == BASE+0xd39878:
            assert [reg(i) for i in (0, 1, 2)] == [OWNER, 2, 0x104]
            trace.append(('read', 0x104, status))
            if read_output:
                u.mem_write(reg(3), struct.pack('<I', status))
            ret(read_error)
        elif address == BASE+0xd39f38:
            assert [reg(i) for i in (0, 1, 2)] == [OWNER, 2, 0x10c]
            trace.append(('write', 0x10c, reg(3)))
            ret(write_error)
        elif address == BASE+0xd38a38:
            ret(OUTER)
        elif address == BASE+0x2385c0:
            ret(0x12345678)
        elif address == BASE+0xd38758:
            ret(0)
        elif address == STUB+4:
            assert reg(1) == PRIMARY
            primary_calls.append(primary_status)
            u.mem_write(reg(2), struct.pack('<I', primary_status))
            ret(primary_error)
        elif address == STUB+8:
            schedules.append(True)
            ret(0)

    u.hook_add(UC_HOOK_CODE, hook)

    def call(rva, *args):
        for i, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        u.emu_start(BASE+rva, STOP, count=20000)
        assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return reg(0)

    call(0xd4aeb8, CTX, mode)
    offsets = {hex(offset): struct.unpack('<I', u.mem_read(CTX+offset, 4))[0]
               for offset in (0x20, 0x28, 0x30, 0x38, 0x40, 0x44, 0x48, 0x4c)}
    for address, value in ((CTX+8, BASE+0xd3d880), (CTX+0x10, BASE+0xd3d8d0),
                           (CTX+0x18, LINK), (LINK, OWNER), (PARENT+0x60, CTX),
                           (ROOTCTX+0x300, PARENT), (ROOTCTX+0x170, PRIMARY),
                           (PRIMARY+0x280, STUB+4), (PRIMARY+0xe0, STUB+8)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(LINK+8, struct.pack('<I', int(enabled)))
    u.mem_write(CTX+0x40e0, struct.pack('<I', counter))
    u.mem_write(CTX+0x40dc, struct.pack('<I', pending))
    result = call(0xd39198, OWNER, ROOTCTX) if parent else call(0xd4b308, CTX)
    return {'return': result, 'trace': trace, 'offsets': offsets,
            'counter': struct.unpack('<I', u.mem_read(CTX+0x40e0, 4))[0],
            'pending': struct.unpack('<I', u.mem_read(CTX+0x40dc, 4))[0],
            'primary_calls': primary_calls, 'schedules': len(schedules),
            'primary_pending': struct.unpack('<I', u.mem_read(PRIMARY+0x4a0, 4))[0],
            'timestamps': {hex(offset): struct.unpack('<Q', u.mem_read(OWNER+offset, 8))[0]
                           for offset in (0x340, 0x348, 0x3c0, 0x3c8, 0x440, 0x448)}}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    header = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/port/pm/nvdla_pm_hfrp_reg.h'
    assert hashlib.sha256(header.read_bytes()).hexdigest() == HEADER_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-') as tmp:
        library, shim = Path(tmp)/'hfrp.so', Path(tmp)/'l4t.c'
        shim.write_text('#include <stdint.h>\n#include "'+str(header)+'"\n'+
                        'void l4t_offsets(uint32_t *v) {\n'+
                        '\n'.join('v[%d]=%s;' % (i, expr) for i, expr in enumerate((
                            'hfrp_buffer_clientoffs_r()', 'hfrp_buffer_cmd_r(0)',
                            'hfrp_buffer_serveroffs_r()', 'hfrp_buffer_resp_r(0)',
                            'hfrp_irq_in_set_r()', 'hfrp_irq_out_set_r()',
                            'hfrp_irq_in_clr_r()', 'hfrp_irq_out_clr_r()')))+'\n}\n')
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_hfrp.c'), str(shim),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    take = lib.n1x_hfrp_take_irq
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Result)]
    offsets = (ctypes.c_uint32*8)()
    lib.l4t_offsets(offsets)
    reference = windows(pe, 0)
    assert list(reference['offsets'].values()) == list(offsets)
    cases = 0
    for status in (0, 1, 2, 3, *(1 << bit for bit in range(26, 32)), 0xfc000003, 0xffffffff):
        for counter in (0, 0xffffffff):
            original = windows(pe, status, counter=counter, pending=0x10)
            assert original['return'] == bool(status)
            assert original['trace'] == [('read', 0x104, status), ('write', 0x10c, status)]
            assert original['counter'] == (counter+bool(status & 2)) & 0xffffffff
            assert original['pending'] == status | 0x10
            for fail_at in (None, 0, 1):
                trace = []

                @READ
                def read(_, offset, out):
                    assert offset == 0x104
                    trace.append(('read', offset, status))
                    out[0] = status
                    return -errno.EIO if fail_at == 0 else 0

                @WRITE
                def write(_, offset, value):
                    assert offset == 0x10c and value == status
                    trace.append(('write', offset, value))
                    return -errno.EIO if fail_at == 1 else 0

                io, result = IO(None, write, read, DELAY()), Result()
                ret = take(ctypes.byref(io), ctypes.byref(result))
                expected = -errno.EIO if fail_at == 0 or (status and fail_at == 1) else (0 if status else -errno.EAGAIN)
                assert ret == expected
                assert result.status_valid == (fail_at != 0)
                assert result.ack_attempted == (fail_at != 0 and bool(status))
                assert result.acknowledged == (fail_at is None and bool(status))
                if result.status_valid:
                    assert result.status == status
                if status and fail_at is None:
                    assert trace == original['trace']
                else:
                    assert trace == original['trace'][:1 if not status or fail_at == 0 else 2]
                cases += 1
    # Windows swallows the lower accessor status; Linux must not do that.
    assert windows(pe, 2, read_error=0x65)['return'] == 1
    assert windows(pe, 2, read_error=0x65, read_output=False)['return'] == 0
    assert windows(pe, 2, write_error=0x65)['return'] == 1
    parent_cases = []
    for enabled in (False, True):
        for status in (0, 2, *(1 << bit for bit in range(26, 32))):
            for primary in (0, 0x40, 0xc0):
                result = windows(pe, status, parent=True, primary_status=primary, enabled=enabled)
                primary_probed = not (enabled and status)
                assert result['primary_calls'] == ([primary] if primary_probed else [])
                assert result['schedules'] == (primary_probed and bool(primary))
                assert result['primary_pending'] == (primary if primary_probed else 0)
                for bit, offset in enumerate((0x340, 0x348, 0x3c0, 0x3c8, 0x440, 0x448), 26):
                    assert result['timestamps'][hex(offset)] == (0x12345678 if enabled and status & (1 << bit) else 0)
                parent_cases.append({'hfrp_enabled': enabled, 'hfrp_status': hex(status),
                                     'primary_status': hex(primary), 'primary_probed': primary_probed})
    accumulated = windows(pe, 1 << 27, pending=1 << 26, parent=True)
    assert accumulated['timestamps']['0x340'] == accumulated['timestamps']['0x348'] == 0x12345678
    result = windows(pe, 0, parent=True, primary_status=0xc0, primary_error=0x65)
    assert result['schedules'] == 0 and result['primary_pending'] == 0
    for invalid in ('io', 'read', 'write', 'result'):
        io, result = IO(None, WRITE(), READ(), DELAY()), Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        if invalid != 'read': io.read32 = READ(lambda _, off, out: 0)
        if invalid != 'write': io.write32 = WRITE(lambda _, off, value: 0)
        assert take(None if invalid == 'io' else ctypes.byref(io),
                    None if invalid == 'result' else ctypes.byref(result)) == -errno.EINVAL
        assert bytes(result) == before
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_initializer': '0xd4aeb8', 'windows_irq_capture': '0xd4b308',
                      'windows_parent_dispatch': '0xd39198', 'l4t_header_sha256': HEADER_HASH,
                      'eight_matching_offsets': dict(zip(reference['offsets'], list(offsets))),
                      'capture_fault_cases': cases, 'parent_dispatch_cases': parent_cases,
                      'limitations': ['All OS/MMIO reads/writes, primary-status accessor and scheduling are synthetic',
                                      'No ACPI IRQ number or GIC delivery is established',
                                      'HFRP event capture does not implement power, clock or response-ring processing',
                                      'The C helper is not linked to the Linux bootstrap or any live IRQ handler',
                                      'No physical boot, power transitions or inference']}, indent=2))


if __name__ == '__main__':
    main()
