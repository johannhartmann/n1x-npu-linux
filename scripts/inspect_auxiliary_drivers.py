#!/usr/bin/env python3
"""Verify the NPU auxiliary interface provider and its IOCTL registration offline."""
import hashlib
import json
import struct
import uuid
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs

ROOT = Path(__file__).resolve().parents[1]
GUID = uuid.UUID('94f86b15-22a9-4607-b95b-3b05184ae8f7')
BASE, CTX, FRAME, STATE, STUB = 0x140000000, 0x21000000, 0x21100000, 0x21200000, 0x21300000
INPUTS = {
    'nvSCP.sys': ('d1cc1ae1a907d607ad979e84116a5febdb2f55136b612b210c68fdd57f901a47', 0x27c38),
    'nvsrootmbx.sys': ('f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a', 0x197e0),
}


def registration(pe, fail=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page, size in ((0x30000, 0x1000), (0x19000, 0x1000)):
        u.mem_map(BASE+page, size)
        u.mem_write(BASE+page, pe.get_data(page, size))
    for address in (CTX, FRAME, STATE, STUB):
        u.mem_map(address, 0x1000)
    for address in (STUB, STUB+0x100, STUB+0x110):
        u.mem_write(address, bytes.fromhex('c0035fd6'))
    table = STUB+0x400
    u.mem_write(CTX+0x838, struct.pack('<Q', table))
    u.mem_write(CTX+0x840, struct.pack('<Q', 0x1234))
    u.mem_write(FRAME+0x18, struct.pack('<Q', 0xabcdef00))
    u.mem_write(table+77*8, struct.pack('<Q', STUB+0x100))
    u.mem_write(table+78*8, struct.pack('<Q', STUB+0x110))
    u.mem_write(BASE+0x19200, struct.pack('<Q', STUB))
    calls = []

    def reg(n):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))

    def hook(_, address, size, user):
        if address in (STUB+0x100, STUB+0x110):
            assert reg(0) == 0x1234 and reg(1) == 0xabcdef00 and reg(3) == 0
            assert bytes(u.mem_read(reg(2), 16)) == GUID.bytes_le
            index = 77 if address == STUB+0x100 else 78
            calls.append({'wdf_index': index, 'guid': str(GUID),
                          'enabled': reg(4) if index == 78 else None})
            u.reg_write(regs.UC_ARM64_REG_X0, 0xc0000001 if fail and index == 77 else 0)

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X24, CTX), (regs.UC_ARM64_REG_X20, CTX),
                            (regs.UC_ARM64_REG_X29, FRAME), (regs.UC_ARM64_REG_X25, STATE)):
        u.reg_write(register, value)
    u.emu_start(BASE+0x30d00, BASE+0x30d70, count=1000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x30d70
    enabled = struct.unpack('<I', u.mem_read(STATE+0x90, 4))[0]
    assert [c['wdf_index'] for c in calls] == ([77] if fail else [77, 78])
    assert enabled == (0 if fail else 1)
    return {'calls': calls, 'local_enabled_flag': enabled}


def main():
    rows = []
    provider = None
    for name, (digest, guid_rva) in INPUTS.items():
        paths = list((ROOT/'sources/platform-drivers').rglob(name))
        assert len(paths) == 1
        path = paths[0]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        pe = pefile.PE(str(path))
        assert pe.get_data(guid_rva, 16) == GUID.bytes_le
        version = pe.VS_FIXEDFILEINFO[0]
        ver = [version.FileVersionMS >> 16, version.FileVersionMS & 65535,
               version.FileVersionLS >> 16, version.FileVersionLS & 65535]
        rows.append({'file': name, 'sha256': digest, 'guid_rva': hex(guid_rva), 'file_version': ver})
        if name == 'nvsrootmbx.sys':
            provider = pe
    records = []
    for rva, code, minimum in ((0x1b090, 0x150214, 32), (0x1b0b0, 0x150218, 0)):
        raw = provider.get_data(rva, 32)
        actual, input_min, output_min = struct.unpack_from('<III', raw)
        handler = struct.unpack_from('<Q', raw, 16)[0]
        assert (actual, input_min, output_min, handler) == (code, minimum, 0, BASE+0x321e0)
        records.append({'ioctl': hex(code), 'minimum_input_bytes': input_min,
                        'minimum_output_bytes': output_min, 'handler_rva': '0x321e0'})
    print(json.dumps({'passed': True, 'hardware_access': False, 'interface_guid': str(GUID),
                      'inputs': rows, 'provider': 'nvsrootmbx.sys / ACPI NVDA8600',
                      'registration_success': registration(provider), 'registration_failure': registration(provider, True),
                      'ioctl_records': records,
                      'limitations': ['Registration runs as a slice with synthetic WDF handles/table and OS stubs',
                                      'Does not execute IOCTL callbacks, mailbox operations or device initialization',
                                      'nvSCP GUID occurrence alone establishes no provider role']}, indent=2))


if __name__ == '__main__':
    main()
