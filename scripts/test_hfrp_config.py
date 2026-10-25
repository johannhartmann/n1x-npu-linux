#!/usr/bin/env python3
"""Replay original Windows HFRP startup/configuration offline in Unicorn.

Mode setter, SRAM packet builder, CONFIG, VF decoder and tuning run unmodified.
OS registry/MMIO, handshake/version, transport and a trailing callback are stubs.
All frequencies, tuning words and VF replies below are synthetic test inputs.
"""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
OUTER, HAL, HFRP, PROVIDER, LINK, OWNER, GLOBAL, SETTINGS, STACK, STUB = (
    0x38000000+i*0x10000 for i in range(10))
STOP = STUB+0xff0
TUNING = tuple(0x10203000+i for i in range(9))
CURVE = ((700, 400), (850, 900), (800, 600))


def run(pe, *, version=(1, 5), mode=None, dvfs=True, sram=True,
        transport_failure=None, firmware_failure=None, entry=0xd41b50,
        curve=CURVE, override_delays=None):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd41000, 0xd44000, 0xd45000, 0xd49000, 0xd3d000, 0xd39000,
                 0xd3b000, 0xd57000, 0xcd000, 0xce000, 0x108f000, 0x1512000,
                 0x13ac000, 0xe3e000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (OUTER, HAL, HFRP, PROVIDER, LINK, OWNER, GLOBAL, SETTINGS, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)

    def put(address, value, fmt='<I'):
        u.mem_write(address, struct.pack(fmt, value))

    for address, value in ((OUTER+0x300, HFRP), (OUTER+0x170, HAL), (HFRP+0x60, PROVIDER),
                           (PROVIDER+0x18, LINK), (LINK, OWNER), (BASE+0x1512148, GLOBAL),
                           (GLOBAL+0x78, SETTINGS), (HAL+0x200, BASE+0xd456e0),
                           (HAL+0x208, BASE+0xd49900), (BASE+0xe3eaf0, STUB), (OUTER+0x118, STUB+0x10)):
        put(address, value, '<Q')
    for address, value in ((LINK+8, 1), (HAL+0x490, int(dvfs)), (HAL+0x5a0, 1),
                           (HAL+0x5a4, 4), (HAL+0x5a8, 10), (SETTINGS+0xa8, 650000),
                           (SETTINGS+0xac, 500000), (SETTINGS+0xb0, 600000),
                           (SETTINGS+0xbc, 0x12345678), (BASE+0x13ac13c, 7)):
        put(address, value)
    u.mem_write(LINK+0xc, bytes(version))
    u.mem_write(HAL+0x474, bytes([int(sram)]))
    for offset, value in ((0x31f, 1), (0x322, 3), (0x325, 15)):
        u.mem_write(OUTER+offset, bytes([value]))
    u.mem_write(SETTINGS+0xc0, struct.pack('<9I', *TUNING))
    events = []

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def ret(value=0):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == STUB:  # CFG validation must preserve argument registers.
            u.reg_write(regs.UC_ARM64_REG_PC, reg(30))
        elif address == STUB+0x10:
            events.append({'trailing_callback': True})
            ret()
        elif address == BASE+0xd39160:
            assert reg(0) == GLOBAL
            ret(GLOBAL)
        elif address == BASE+0xd3b8c0:
            name = bytes(u.mem_read(reg(2), 64)).split(b'\0', 1)[0].decode()
            events.append({'registry': name})
            value = mode if name == 'RmSocDlaHfrpMode' else (override_delays or {}).get(name)
            if value is not None:
                put(reg(3), value)
            ret(0 if value is not None else 1)
        elif address == BASE+0xd39f38:
            assert (reg(0), reg(1), reg(2), reg(3)) == (OWNER, 2, 4, 1)
            events.append({'resource': 2, 'offset': 4, 'value': 1})
            ret()
        elif address == BASE+0xd3da30:
            assert reg(0) == HFRP
            events.append({'handshake_version_stub': True})
            ret()
        elif address == BASE+0xcd148:
            assert reg(1) == 0 and reg(2) <= 40
            u.mem_write(reg(0), bytes(reg(2)))
            ret()
        elif address == BASE+0xd3de30:
            assert reg(0) == HFRP
            command, length = reg(1), reg(3)
            packet = bytes(u.mem_read(reg(2), length)) if length else b''
            events.append({'command': command, 'payload': packet.hex()})
            response = (struct.pack('<H', len(curve))+b''.join(struct.pack('<HH', *pair) for pair in curve)
                        if command == 428 else bytes(length))
            assert len(response) <= (34 if command == 428 else length)
            if response:
                u.mem_write(reg(5), response)
            put(reg(4), 1 if firmware_failure == command else 0, '<H')
            put(reg(6), len(response))
            ret(0x65 if transport_failure == command else 0)
        elif address in (BASE+0xce4b0, BASE+0xce4e8):
            ret()

    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in ((0, OUTER), (1, HAL), (19, OUTER), (20, HAL), (21, GLOBAL), (22, 1)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    stop = BASE+0xd44508 if entry == 0xd44488 else STOP
    try:
        u.emu_start(BASE+entry, stop, count=100000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}; events={events}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == stop
    return {'status': reg(0), 'events': events,
            'mode': int.from_bytes(u.mem_read(LINK+8, 4), 'little'),
            'delays': list(struct.unpack('<3I', u.mem_read(HAL+0x5a0, 12))),
            'vf_count': int.from_bytes(u.mem_read(HAL+0x528, 2), 'little'),
            'vf_limits': list(struct.unpack('<4I', u.mem_read(HAL+0x52c, 16))),
            'frequency_range_selected': list(struct.unpack('<3I', u.mem_read(HAL+0x57c, 12)))}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for minor in (3, 5, 6):
        for dvfs in (False, True):
            for sram in (False, True):
                result = run(pe, version=(1, minor), dvfs=dvfs, sram=sram)
                commands = [e for e in result['events'] if 'command' in e]
                config = struct.pack('<HHIIH', 4, 10, 500000 if dvfs else 0, 600000 if dvfs else 0, 1)
                if minor >= 5:
                    config += struct.pack('<6H', 0, 0, 1, 1, 3, 15)
                expected = [(412, '02' if sram else '01'), (401, config.hex()), (428, '')]
                if dvfs:
                    expected += [(408, struct.pack('<9I', *TUNING).hex()), (407, '78563412')]
                assert [(e['command'], e['payload']) for e in commands] == expected, result
                assert result['status'] == 0 and result['vf_count'] == 3
                assert result['vf_limits'] == [400000, 900000, 700, 850]
                assert result['frequency_range_selected'] == ([400000, 900000, 650000] if dvfs else [0, 0, 0])
                assert result['events'][:3] == [{'registry': 'RmSocDlaHfrpMode'},
                    {'resource': 2, 'offset': 4, 'value': 1}, {'handshake_version_stub': True}]
                rows.append({'minor': minor, 'dvfs': dvfs, 'sram': sram, **result})
    for mode in (0, 2):
        result = run(pe, mode=mode)
        assert result['mode'] == mode and result['status'] == 0
        assert not any('command' in e or 'handshake_version_stub' in e for e in result['events'])
        assert sum('resource' in e for e in result['events']) == (mode != 0)
        rows.append({'mode_override': mode, **result})
    for command in (401, 428, 408, 407):
        for error in ('transport_failure', 'firmware_failure'):
            direct = run(pe, entry=0xd456e0, **{error: command})
            parent = run(pe, **{error: command})
            # Windows parent discards configuration errors; SET_TUNING's caller
            # also discards its failure. Linux must not copy this behavior.
            assert parent['status'] == 0
            expected = (0x65 if error == 'transport_failure' else 0xffff) if command == 407 else (
                0x65 if error == 'transport_failure' and command in (401, 428) else 0)
            assert direct['status'] == expected, (command, error, direct)
            rows.append({'failed_command': command, 'failure': error,
                         'direct_status': direct['status'], 'parent_status': parent['status']})
    for overrides, expected in ((None, [1, 4, 10]),
                                ({'RmSocDlaDelayFlcg': 7, 'RmSocDlaDelayElpg': 8,
                                  'RmSocDlaDelayDlarg': 9}, [7, 8, 9])):
        result = run(pe, entry=0xd44488, override_delays=overrides)
        assert result['delays'] == expected, result
        rows.append({'delay_defaults_slice': True, 'overrides': overrides, **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'cases': rows, 'limitations': [
                          'Synthetic DVFS settings, tuning words and curve; no calibrated defaults',
                          'OS, resource writer, handshake/version, transport and trailing callback stubbed',
                          'Delay slice starts with w22=1 as observed earlier in its caller',
                          'No physical HFRP configuration or firmware boot attempted']}, indent=2))


if __name__ == '__main__':
    main()
