#!/usr/bin/env python3
"""Audit the exact builtin diagnostic's Windows selector and binary references."""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from inspect_loadable import Buffer, inspect
from diagnostic_fixture import DIAGNOSTIC_HASH, build
from emulate_profile1_commands import HOST

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_HASH = 'c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b'
BASE = 0x180000000
CTX, VTABLE, OUT, STACK, STUB = (0x3a000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


def append(pe, selector, model_status=0, log_status=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0x15000, 0x6b000, 0x79000, 0x96000, 0xa5000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    # Map the actual complete vendor bytes used by the original pointer getter.
    start, size = 0xa6000, (0x610+0x5a9fc8+4095)&~4095
    u.mem_map(BASE+start, size)
    u.mem_write(BASE+start, pe.get_data(start, size))
    for address in (CTX, VTABLE, OUT, STACK, STUB):
        u.mem_map(address, 0x10000)
    u.mem_write(CTX, struct.pack('<Q', VTABLE))
    u.mem_write(CTX+0x98, struct.pack('<I', selector))
    u.mem_write(VTABLE+0x218, struct.pack('<Q', STUB+0x40))
    u.mem_write(BASE+0xa5340, struct.pack('<Q', STUB+0x20))
    u.mem_write(OUT, b'\xa5'*4)
    events = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    def hook(_, address, size, user):
        if address == BASE+0x6b290:
            assert reg(0) == CTX+8
            events.append('lock')
            ret(0)
        elif address == BASE+0x6b2d8:
            assert reg(0) == CTX+8
            events.append('unlock')
            ret(0)
        elif address == BASE+0x79ac0:
            assert reg(0) == 0 and reg(1) == BASE+0x96948
            events.append('log')
            ret(log_status)
        elif address == STUB+0x20:
            # Explicit CFG validation boundary; preserve x15 call target.
            u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))
        elif address == STUB+0x40:
            assert [reg(i) for i in range(4)] == [CTX, BASE+0xa6610, 0x5a9fc8, OUT]
            assert hashlib.sha256(bytes(u.mem_read(reg(1), reg(2)))).hexdigest() == DIAGNOSTIC_HASH
            events.append('append-model')
            if not model_status:
                u.mem_write(OUT, struct.pack('<I', 42))
            ret(model_status)

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_X1, OUT)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x15eb8, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'selector': selector, 'status': reg(0), 'events': events,
            'output_handle': struct.unpack('<I', u.mem_read(OUT, 4))[0]}


def main():
    runtime = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(runtime.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(runtime), fast_load=True)
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == DIAGNOSTIC_HASH
    assert pe.get_data(0xa6610, 0x5a9fc8) == data
    cases = []
    for selector in (0, 1, 2, 3, 4, 0xffffffff):
        row = append(pe, selector)
        assert row['events'] == ['lock', 'log']+(['append-model'] if selector == 1 else [])+['unlock']
        assert row['status'] == (0 if selector == 1 else 2)
        assert row['output_handle'] == (42 if selector == 1 else 0xa5a5a5a5)
        cases.append(row)
    row = append(pe, 1, model_status=0x65)
    assert row['status'] == 0x65 and row['output_handle'] == 0xa5a5a5a5
    cases.append(row)
    row = append(pe, 1, log_status=0x65)
    assert row['status'] == 0x65 and row['events'] == ['lock', 'log', 'unlock']
    assert row['output_handle'] == 0xa5a5a5a5
    cases.append(row)
    view, metadata = Buffer(data), inspect(data)
    blobs = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        blobs[view.string(table, 0)] = data[extent.start:extent.stop]
    payload, addresses, layout = build(data, HOST)
    assert len(layout) == len(addresses) == 9
    tensors = []
    for table in view.tables(view.u('<I', 0), 6):
        # These are observed table slots and raw names, not a general schema.
        name = view.string(table, 0)
        memory_id = view.scalar(table, 2, '<H')
        size = view.scalar(table, 3, '<Q')
        assert size == layout[memory_id]['bytes']
        tensors.append({'raw_name': name, 'slot_1_u16': view.scalar(table, 1, '<H'),
                        'slot_2_u16_memory_id': memory_id, 'slot_3_u64_bytes': size})
    assert [t['raw_name'] for t in tensors] == ['task0_stats', 'working_buffer', 'scratch_buffer', 'positive', 'negative', 'sdl_out']
    assert tensors[-1]['slot_2_u16_memory_id'] == 8 and tensors[-1]['slot_3_u64_bytes'] == 32
    assert blobs['negative'] == bytes(27264)
    assert blobs['positive'] != blobs['task0_stats']
    # The identical metadata prefix is a raw observation; do not compare an
    # entire future stats result against the whole positive blob as a verdict.
    prefix = 4+25*704
    assert struct.unpack_from('<I', blobs['positive'])[0] == 25
    assert blobs['positive'][:prefix] == blobs['task0_stats'][:prefix]
    names = []
    for i in range(25):
        start = 4+i*704
        raw = blobs['positive'][start:start+704].split(b'\0', 1)[0]
        assert raw.startswith(b'fi_') and len(raw) < 704
        names.append(raw.decode('ascii'))
    first_difference = next(i for i, (a, b) in enumerate(zip(blobs['positive'], blobs['task0_stats'])) if a != b)
    print(json.dumps({'passed': True, 'hardware_access': False, 'runtime_sha256': RUNTIME_HASH,
                      'loadable_sha256': DIAGNOSTIC_HASH, 'windows_append': '0x15eb8',
                      'windows_selector_offset': '0x98', 'append_cases': cases,
                      'tensor_slot_observations': tensors, 'fixture_backing_bytes': len(payload),
                      'blobs': [{'name': n, 'bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n, b in blobs.items()],
                      'reference_name_count': len(names), 'reference_names': names,
                      'stats_positive_identical_prefix_bytes': prefix,
                      'stats_positive_first_difference': first_difference,
                      'limitations': ['Windows lock/log/CFG/model-parser boundaries are synthetic; no task executes',
                                      'Modern tensor table meanings beyond observed slots remain provisional',
                                      'Positive/negative names and raw differences do not prove comparison algorithm or output bits',
                                      'No golden sdl_out result, physical completion or numerical correctness established']}, indent=2))


if __name__ == '__main__':
    main()
