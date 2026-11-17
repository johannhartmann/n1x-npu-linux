#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the pinned Windows N1X outer initializer against explicit OS stubs.

Optional research tool: requires pefile and Unicorn, never accesses a device.
It tests vendor error propagation, not successful hardware initialization.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs

from prepare_inputs import MANUAL, ROOT

BASE = 0x140000000
ADAPTER, OUTER, HAL, STACK, STUB, MANAGER = (0x39000000 + i * 0x10000 for i in range(6))
HFRP = MANAGER + 0x2000
TRANSPORT = MANAGER + 0x3000
LINK = MANAGER + 0x8000
STOP = STUB + 0xff0


def firmware_inventory(raw):
    """Inventory signed native envelopes embedded in the exact pinned KMD."""
    rows, cursor = [], 0
    while True:
        offset = raw.find(b'\x02\x03\x0c\x00', cursor)
        if offset < 0:
            break
        cursor = offset + 1
        if offset + 7 > len(raw):
            continue
        prefix = 7 + int.from_bytes(raw[offset + 5:offset + 7], 'big')
        native = offset + prefix
        if native + 76 > len(raw) or struct.unpack_from('<3I', raw, native) != (1, 2, 0xefbeadde):
            continue
        section = struct.unpack_from('<8I', raw, native + 44)
        if section[:2] != (0xefbeadde, 0xc0302):
            continue
        total = prefix + section[3] + section[4] + section[5]
        if total <= prefix or offset + total > len(raw):
            continue
        envelope = raw[offset:offset + total]
        inner_offset = native + section[3] + section[4]
        inner = raw[inner_offset:inner_offset + section[5]]
        plaintext = len(inner) >= 24 and struct.unpack_from('<I', inner)[0] == 0x10fe
        inner_digest = None
        if plaintext:
            magic, version, size, header, payload_offset, payload_size = struct.unpack_from('<6I', inner)
            if (magic, version, size) != (0x10fe, 1, len(inner)) or header < 24:
                raise ValueError('Malformed plaintext native payload in pinned KMD')
            if payload_offset + payload_size > len(inner):
                raise ValueError('Out-of-bounds plaintext native payload in pinned KMD')
            inner_digest = hashlib.sha256(inner).hexdigest()
        rows.append({'file_offset': hex(offset), 'bytes': total,
                     'sha256': hashlib.sha256(envelope).hexdigest(),
                     'plaintext_inner_sha256': inner_digest,
                     'hfrp_busy_string': envelope.find(b'HFRP is busy') >= 0})
    plaintext = [row for row in rows if row['plaintext_inner_sha256']]
    assert len(rows) == 10 and len(plaintext) == 6
    assert {row['plaintext_inner_sha256'] for row in plaintext} == {
        '2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997',
        '0a1578fb069afb8e9bacdda5b93a3c56ed4f1f76477e3531791bb7665f7f8d79',
    }
    return {
        'native_envelopes': len(rows),
        'plaintext_envelopes': len(plaintext),
        'unique_plaintext_images': 2,
        'images': rows,
    }


def replay(pe, *, failure=None, phase=0, hfrp_mode=1, hfrp_context=True):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0xd38000, 0x13000), (0x194f000, 0xf000),
                      (0x108e000, 0x2000), (0xe3e000, 0x1000),
                      (0x5ee000, 0x1000), (0x142000, 0x1000),
                      (0x5000, 0x1000), (0xce000, 0x1000),
                      (0xd57000, 0x1000), (0x11ff000, 0x1000),
                      (0x13ac000, 0x1000), (0xeb2000, 0x1000),
                      (0x14000, 0x1000), (0xfb000, 0x1000), (0xed000, 0x1000),
                      (0xcd000, 0x1000), (0x26c000, 0x1000),
                      (0x1964000, 0x2000), (0x1512000, 0x1000)):
        uc.mem_map(BASE + rva, size)
        uc.mem_write(BASE + rva, pe.get_data(rva, size))
    for address in (ADAPTER, OUTER, HAL, STACK, STUB, MANAGER):
        uc.mem_map(address, 0x10000)

    def put(address, value, fmt='<Q'):
        uc.mem_write(address, struct.pack(fmt, value))

    def register(index):
        return uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def execute(entry):
        uc.reg_write(regs.UC_ARM64_REG_LR, STOP)
        uc.reg_write(regs.UC_ARM64_REG_SP, STACK + 0x8000)
        try:
            uc.emu_start(BASE + entry, STOP, count=100000)
        except Exception as exc:
            raise RuntimeError(f'Unmodeled edge at PC={uc.reg_read(regs.UC_ARM64_REG_PC):#x}') from exc
        assert uc.reg_read(regs.UC_ARM64_REG_PC) == STOP, 'Instruction budget exhausted'

    # Execute the real profile/HAL callback constructors; model allocation and
    # base-object bookkeeping. Keep the original stack-cookie frame helpers.
    put(BASE + 0x11ff5c0, 0x12345678)
    put(OUTER + 0x14e, 3, '<H')
    put(HAL + 0x496, 1, '<H')
    for address, entry in ((OUTER, 0x194fb08), (HAL, 0x1951238)):
        uc.reg_write(regs.UC_ARM64_REG_X0, address)
        execute(entry)
    put(HAL, BASE + 0x108e9a0)
    put(HAL + 8, BASE + 0x108e9b8)
    put(HAL + 0x68, HAL + 8)
    put(OUTER + 0x170, HAL)
    put(OUTER + 0x178, HAL + 8)
    put(OUTER + 0x310, phase, '<I')
    put(ADAPTER + 0x68, MANAGER)
    put(BASE + 0x1512148, MANAGER)
    put(BASE + 0xe3eaf0, STUB)
    uc.mem_write(STUB, bytes.fromhex('c0035fd6'))

    boundaries = {
        0x143f0: 'object_kind', 0xd3b5c8: 'object_selector',
        0x1950ec8: 'allocate_outer', 0xd3b8c0: 'query_policy',
        0xed2d0: 'allocate_callback_record', 0xd3b6b0: 'query_metadata',
        0xd3d2c0: 'metadata_fallback', 0xd3b530: 'named_service_setup',
        0xd440d0: 'registry', 0xd39160: 'registry_lookup',
        0xd39f38: 'generic_write', 0xd39878: 'generic_read',
        0xd4af88: 'handshake', 0xd3de30: 'transport_command',
        0xcd148: 'zero_memory', 0x26cbc0: 'mutex_init',
        0x5be0: 'base_init', 0xfbbe8: 'registration_lookup',
        0xfbee0: 'registration_attach', 0xed3f0: 'sync_prepare',
        0xedfc8: 'sync_allocate', 0xd3b008: 'hfrp_aux',
        0xd44710: 'gating_policy', 0xd43730: 'resume_setup',
        0xd405f0: 'power_on', 0xd439c0: 'loader',
        0xd3b1d8: 'release_outer', 0xfbce0: 'free', 0xd39b30: 'stop',
    }
    cleanup = int.from_bytes(uc.mem_read(HAL + 0xd0, 8), 'little') - BASE
    assert cleanup not in boundaries
    boundaries[cleanup] = 'cleanup'
    ready_callback = int.from_bytes(uc.mem_read(OUTER + 0x100, 8), 'little')
    boundaries[ready_callback - BASE] = 'initialized_callback'
    hfrp_policy_setup = int.from_bytes(uc.mem_read(HAL + 0x200, 8), 'little')
    boundaries[hfrp_policy_setup - BASE] = 'hfrp_policy_setup'
    hfrp_ready_callback = int.from_bytes(uc.mem_read(OUTER + 0x118, 8), 'little')
    boundaries[hfrp_ready_callback - BASE] = 'hfrp_ready_callback'
    events = []
    sync_handles = iter((MANAGER + 0x9000, MANAGER + 0xa000))

    def hook(_, address, size, user):
        rva = address - BASE
        if rva == 0xd3b3b0:
            events.append('callback_registration')
            return  # Execute the original body, including its allocation gate.
        if rva not in boundaries and rva not in (0xd38a38, 0xd38758, 0xce4b0, 0xce4e8):
            return
        if rva not in boundaries:
            result = 0
        else:
            name = boundaries[rva]
            if name == 'power_on' and (hfrp_mode == 0 or not hfrp_context):
                events.append('power_on_original')
                return  # The original no-transport branch is self-contained.
            if name == 'allocate_callback_record':
                size = register(0)
                allocations = {
                    0x68: ('allocate_hfrp', HFRP),
                    0x40e8: ('allocate_transport', TRANSPORT),
                    0x10: ('allocate_link', LINK),
                    0x18: ('allocate_callback_record', MANAGER + 0x1000),
                }
                assert size in allocations
                name, allocation = allocations[size]
            events.append(name)
            result = 0x65 if failure == name else 0
            if name == 'object_kind':
                result = 0
            elif name == 'object_selector':
                result = 0x34
            elif name == 'allocate_outer':
                assert register(4) == 0x71
                put(register(0), OUTER)
            elif name == 'allocate_callback_record':
                assert register(0) == 0x18
                result = 0 if failure == name else MANAGER + 0x1000
            elif name.startswith('allocate_'):
                result = allocation
            elif name == 'query_policy':
                key = register(2)
                if key == BASE + 0x108f1b0:
                    put(register(3), hfrp_mode, '<I')
                    result = 0
                else:
                    result = 1  # Explicit absent optional-policy branch.
            elif name == 'query_metadata':
                result = 1  # Explicit absent-property/fallback branch.
            elif name == 'registry_lookup':
                result = MANAGER
            elif name == 'generic_write':
                assert (register(0), register(1), register(2), register(3)) == (OUTER, 2, 4, 1)
            elif name == 'generic_read':
                assert (register(0), register(1), register(2)) == (OUTER, 2, 0)
                put(register(3), 0x20251722, '<I')
            elif name == 'handshake':
                assert register(0) == TRANSPORT
            elif name == 'transport_command':
                assert register(0) == HFRP and register(1) == 0x19c and register(3) == 1
            elif name == 'zero_memory':
                uc.mem_write(register(0), bytes([register(1) & 0xff]) * register(2))
                result = register(0)
            elif name == 'registration_lookup':
                result = MANAGER + 0xb000
            elif name == 'sync_allocate':
                result = next(sync_handles)
            elif name == 'hfrp_aux':
                result = MANAGER + 0xc000
            elif name == 'free':
                pass  # Synthetic allocation; never frees a host/kernel pointer.
        uc.reg_write(regs.UC_ARM64_REG_X0, result)
        uc.reg_write(regs.UC_ARM64_REG_PC, register(30))

    uc.hook_add(UC_HOOK_CODE, hook)
    if hfrp_context:
        # Execute the original outer wrapper, object factory, HFRP initializer
        # and transport constructor. Only allocators/OS primitives are modeled.
        uc.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        execute(0xd3f6d0)
        assert register(0) == 0
        assert int.from_bytes(uc.mem_read(OUTER + 0x300, 8), 'little') == HFRP
        assert int.from_bytes(uc.mem_read(HFRP + 0x60, 8), 'little') == TRANSPORT
        assert int.from_bytes(uc.mem_read(TRANSPORT + 0x18, 8), 'little') == LINK
        assert int.from_bytes(uc.mem_read(LINK, 8), 'little') == OUTER
        put(TRANSPORT + 0x34, 0x20251722, '<I')
    for index, value in ((0, ADAPTER), (1, 0x1234), (2, 0), (3, 0)):
        uc.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    execute(0xd38a88)
    return {'failure': failure, 'initial_state': phase, 'hfrp_mode': hfrp_mode,
            'hfrp_context': hfrp_context, 'status': register(0), 'events': events,
            'link_mode': int.from_bytes(uc.mem_read(LINK + 8, 4), 'little'),
            'firmware_version': list(uc.mem_read(LINK + 0xc, 2)),
            'loader_stage': int.from_bytes(uc.mem_read(HAL + 0x524, 4), 'little')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / MANUAL[0][0])
    args = parser.parse_args()
    raw = args.input.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != MANUAL[0][1]:
        raise SystemExit('Unexpected vendor input SHA-256; no code executed')
    pe = pefile.PE(data=raw, fast_load=True)
    cases = [replay(pe, failure=failure, phase=phase) for phase in (0, 3) for failure in
             (None, 'allocate_callback_record', 'named_service_setup', 'registry',
              'handshake', 'gating_policy', 'power_on', 'loader')]
    cases += [replay(pe, phase=phase, hfrp_mode=0) for phase in (0, 3)]
    cases += [replay(pe, phase=phase, hfrp_context=False) for phase in (0, 3)]
    for row in cases:
        failure = row['failure']
        if failure == 'gating_policy' or (failure == 'handshake' and row['initial_state'] == 0):
            assert row['status'] == 0 and 'loader' in row['events'], row
        elif failure == 'allocate_callback_record':
            assert row['status'] == 0x1a and 'handshake' not in row['events'], row
        elif failure:
            assert row['status'] == 0x65, row
        elif row['hfrp_context'] and row['hfrp_mode'] == 1:
            assert row['status'] == 0 and row['events'][-1] == 'initialized_callback', row
        else:
            assert row['status'] == 0 and row['events'][-1] == 'initialized_callback', row
            assert 'power_on_original' in row['events'], row
        if not row['hfrp_context']:
            assert not ({'registry_lookup', 'generic_write', 'handshake'} & set(row['events'])), row
        elif row['hfrp_mode'] == 0:
            assert row['link_mode'] == 0 and 'generic_write' not in row['events']
            assert 'handshake' not in row['events'], row
        elif failure is None:
            assert row['link_mode'] == 1 and row['firmware_version'] == [1, 5], row
            assert {'generic_write', 'handshake', 'generic_read'} <= set(row['events']), row
    print(json.dumps({'input_sha256': digest, 'hardware_access': False,
                      'entry_rva': '0xd38a88', 'hfrp_wrapper_rva': '0xd41b50',
                      'hfrp_factory_wrapper_rva': '0xd3f6d0',
                      'hfrp_factory_rva': '0x1964e00',
                      'hfrp_object_initializer_rva': '0xd3d8f0',
                      'transport_constructor_rva': '0xd4aeb8',
                      'mode_selector_rva': '0xd3dd08',
                      'version_discovery_rva': '0xd3da30', 'power_rva': '0xd405f0',
                      'embedded_firmware': firmware_inventory(raw), 'cases': cases,
                      'limitations': [
                          'Original constructors, callback registration, global initializer, '
                          'HFRP wrapper, mode selection and version discovery execute in Unicorn',
                          'Allocation bookkeeping, OS, named service, HFRP transport, policy, '
                          'mode-1 power and loader bodies are modeled',
                          'The original no-transport power branch executes for mode 0 and a '
                          'missing HFRP context',
                          'This does not identify the physical HFRP peer or qualify '
                          'reset/recovery',
                      ]}, indent=2))


if __name__ == '__main__':
    main()
