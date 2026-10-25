#!/usr/bin/env python3
"""Execute original Windows power policy with HFRP/OS calls recorded in RAM."""
import hashlib
import json
import ctypes
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
from emulate_firmware_commands import ROOT

BASE = 0x140000000
OUTER, HFRP, PROVIDER, LINK, STACK, STUB = (0x32000000+i*0x10000 for i in range(6))
STOP = STUB+0xff0


def reference_power_library():
    """Compile the pinned NVIDIA implementation, stubbing only its transport/events."""
    directory = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/port/pm'
    source = (directory/'nvdla_pm_hfrp_cmd_v1_3.c').read_bytes()
    assert hashlib.sha256(source).hexdigest() == '22d224fc2ad2ccf0db5a7c0e2ae72f048a16dd5f49b261aa25bfad64075e8acf'
    header = (directory/'nvdla_pm_hfrp.h').read_text()
    definition = header.split('struct nvdla_hfrp_cmd_power_ctrl {', 1)[1].split('};', 1)[0]
    function = source.decode().split('int32_t nvdla_hfrp_send_cmd_power_ctrl(', 1)[1].split('\nint32_t nvdla_hfrp_send_cmd_config(', 1)[0]
    prelude = '''#include <stdint.h>
#include <stdbool.h>
#include <string.h>
struct hfrp { uint8_t *out; };
#define DLA_HFRP_CMD_POWER_CONTROL 400
#define DLA_HFRP_CMD_POWER_CONTROL_SZ 3U
#define SZ(x) x##_SZ
static int hfrp_send_cmd(struct hfrp *h, int cmd, uint8_t *p, unsigned n, bool blocking)
{ (void)blocking; if (cmd != 400 || n != 3) return -1; memcpy(h->out,p,n); return 0; }
'''
    for event in ('cg', 'pg', 'rg'):
        for direction in ('entry', 'exit'):
            prelude += f'static void hfrp_handle_{event}_{direction}_start(struct hfrp *h) {{ (void)h; }}\n'
    wrapper = '''
int encode(uint16_t bits, uint8_t pps, uint8_t *out) {
    struct hfrp h = { out };
    struct nvdla_hfrp_cmd_power_ctrl cmd = {
        .power_off = !!(bits & 1), .rail_off = !!(bits & 2),
        .power_on = !!(bits & 4), .rail_on = !!(bits & 8),
        .power_delayed_off = !!(bits & 16), .rail_delayed_off = !!(bits & 32),
        .clock_off = !!(bits & 64), .clock_on = !!(bits & 128),
        .clock_delayed_off = !!(bits & 256), .pps = (int8_t)pps
    };
    return nvdla_hfrp_send_cmd_power_ctrl(&h, &cmd, true);
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-reference-') as tmp:
        path = Path(tmp)
        (path/'reference.c').write_text(prelude+'struct nvdla_hfrp_cmd_power_ctrl {'+definition+'};\n'
                                      +'int32_t nvdla_hfrp_send_cmd_power_ctrl('+function+wrapper)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            str(path/'reference.c'), '-o', str(path/'reference.so')], check=True)
        lib = ctypes.CDLL(str(path/'reference.so'))
    lib.encode.argtypes = [ctypes.c_uint16, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8)]
    lib.encode.restype = ctypes.c_int
    return lib


def run(pe, request=7, *, version=(1, 3), capability=1, enabled=7,
        pending=0, guard=0, transport_error=0, firmware_error=0,
        delayed_flags=False, slcg=False, pps=(17, 19, 23, 29), absent=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd40000, 0x1000), (0xd3d000, 0x1000),
                      (0xd3f000, 0x1000), (0xd39000, 0x1000),
                      (0xd38000, 0x1000), (0xd57000, 0x1000),
                      (0x194f000, 0x2000), (0xcd000, 0x2000),
                      (0x13ac000, 0x1000), (0xe3e000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address in (OUTER, HFRP, PROVIDER, LINK, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(OUTER+0x14e, struct.pack('<H', 3))
    u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x194fb08, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    target = struct.unpack('<Q', u.mem_read(OUTER+0xc8, 8))[0]
    assert target == BASE+0xd405f0
    for address, value in ((OUTER+0x300, 0 if absent else HFRP),
                           (HFRP+0x60, PROVIDER), (PROVIDER+0x18, LINK)):
        u.mem_write(address, struct.pack('<Q', value))
    for address, value in ((OUTER+0x4a8, pending), (OUTER+0x4b0, enabled), (LINK+8, capability)):
        u.mem_write(address, struct.pack('<I', value))
    u.mem_write(LINK+0xc, bytes(version))
    u.mem_write(OUTER+0x14c, bytes([int(slcg), int(delayed_flags)]))
    for offset, value in zip((0x31c, 0x31f, 0x322, 0x325), pps):
        u.mem_write(OUTER+offset, bytes([value]))
    # Suppress the optional verbose duration log, without changing policy.
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))
    events = []

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xcd148:
            assert reg(1) == 0 and reg(2) in (1, 3)
            u.mem_write(reg(0), bytes(reg(2)))
            ret()
        elif address in (BASE+0xd3dd40, BASE+0xd3de30):
            assert reg(0) == HFRP
            events.append({'command': hex(reg(1)), 'payload': bytes(u.mem_read(reg(2), reg(3))).hex()})
            u.mem_write(reg(4), struct.pack('<H', firmware_error))
            u.mem_write(reg(5), bytes(reg(3)))
            u.mem_write(reg(6), struct.pack('<I', reg(3)))
            if address == BASE+0xd3dd40:
                u.mem_write(reg(7), struct.pack('<Q', 1000))
                end_pointer = struct.unpack('<Q', u.mem_read(u.reg_read(regs.UC_ARM64_REG_SP), 8))[0]
                u.mem_write(end_pointer, struct.pack('<Q', 2000))
            ret(transport_error)
        elif address == BASE+0xd39ef0:
            assert reg(0) == OUTER and reg(1) == 0
            events.append({'notification_mask': reg(2), 'entering': reg(3)})
            ret()
        elif address in (BASE+0xd38a38, BASE+0xd38970, BASE+0xd3f458,
                          BASE+0xd3f4f0, BASE+0xce440, BASE+0xce4b0):
            ret()

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X0, OUTER), (regs.UC_ARM64_REG_X1, guard),
                            (regs.UC_ARM64_REG_X2, request), (regs.UC_ARM64_REG_SP, STACK+0x8000),
                            (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(register, value)
    try:
        u.emu_start(target, STOP, count=100000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'events': events,
            'pending': int.from_bytes(u.mem_read(OUTER+0x4a8, 4), 'little'),
            'pps': int.from_bytes(u.mem_read(OUTER+0x31c, 1), 'little')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    reference = reference_power_library()
    rows = []

    def check(inputs, payload, status=0):
        result = run(pe, **inputs)
        assert result['status'] == status, (inputs, result)
        commands = [e for e in result['events'] if 'command' in e]
        expected = [] if payload is None else [{'command': '0x190', 'payload': payload}]
        assert commands == expected, (inputs, result)
        rows.append({'inputs': inputs, **result})
        return result

    # Original request bits are policy bits, not HFRP wire bits.
    for request, wire, pps in ((1, 0x80, 19), (2, 4, 19), (4, 0x0c, 19),
                                (7, 0x8c, 19), (0x10, 0x40, 19),
                                (0x20, 1, 23), (0x40, 3, 29),
                                (0x100, 0x100, 19), (0x200, 0x10, 23),
                                (0x400, 0x30, 29)):
        for minor in (3, 5, 6):
            payload = wire.to_bytes(2, 'little') + (bytes([pps]) if minor == 3 else b'')
            if minor == 3:
                output = (ctypes.c_uint8 * 3)()
                assert reference.encode(wire, pps, output) == 0
                assert bytes(output) == payload
            check({'request': request, 'enabled': 0x777, 'version': (1, minor)}, payload.hex())
    check({'request': 0}, None)
    check({'enabled': 0}, None)
    check({'guard': 1}, None, 0x56)
    for request in (0x11, 0x22, 0x44):
        check({'request': request}, None, 0x3f)
    check({'absent': True}, None, 0xffff)
    check({'capability': 0}, None, 0xffff)
    for version in ((1, 2), (1, 4), (2, 3)):
        check({'version': version}, None, 0x40)
    check({'transport_error': 0x65}, '8c0013', 0x65)
    check({'firmware_error': 1}, '8c0013', 0xffff)
    check({'request': 0x10, 'enabled': 0x777, 'pending': 1}, None)
    # Version 1.5 optional SLCG command follows successful clock-on.
    result = run(pe, version=(1, 5), slcg=True)
    assert [e for e in result['events'] if 'command' in e] == [
        {'command': '0x190', 'payload': '8c00'}, {'command': '0x1a1', 'payload': '01'}]
    rows.append({'inputs': {'version': (1, 5), 'slcg': True}, **result})
    for request, payload in ((0x100, '0013'), (0x200, '1024'), (0x400, '306c')):
        check({'request': request, 'enabled': 0x777, 'version': (1, 5),
               'delayed_flags': True}, payload)
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'outer_callback': '0xd405f0', 'cases': rows,
                      'l4t_v13_compiled_reference_comparisons': 10,
                      'limitations': ['Original profile-3 initializer and full power callback run in synthetic RAM',
                                      'Transport, notifications, timing/logging and memset are explicit stubs',
                                      'Capability/version/request inputs are synthetic; physical power effects and live negotiated version are unknown']}, indent=2))


if __name__ == '__main__':
    main()
