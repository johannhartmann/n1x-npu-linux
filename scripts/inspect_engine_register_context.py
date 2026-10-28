#!/usr/bin/env python3
"""Recover banked register-context commands from the original Windows compiler."""
import contextlib
import hashlib
import io
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_dla_compiler import ROOT
from inspect_compiler_context import COMPILER_HASH
from inspect_windows_mtl_collector import main as collect


def encode_range(register, count, save):
    """Recovered producer formula for nonnegative counts and Boolean mode."""
    assert 0 <= register < 1 << 23 and 0 <= count < 1 << 31 and save in (0, 1)
    return register << 2 | (1-save) << 25 | count << 26 | 2


def context_bytes(register, count):
    # Exact compiler accounting at 0xcb9c38..0xcb9c54. No count+1 rule.
    return ((register+count*4+63) & ~63)-(register & ~63)


def trace():
    groups, active, pending = [], None, None

    def observer(u, pe, base):
        def word(address):
            return int.from_bytes(u.mem_read(address, 4), 'little')

        def code(u, pc, size, user):
            nonlocal active, pending
            pc -= base
            if pc == 0xcb99f8:
                assert active is None and pending is None
                vector = u.reg_read(r.UC_ARM64_REG_X3)
                counter = u.reg_read(r.UC_ARM64_REG_X4)
                begin, end, capacity = struct.unpack('<3Q', u.mem_read(vector, 24))
                assert begin <= end <= capacity
                active = {'engine_id': u.reg_read(r.UC_ARM64_REG_W1),
                          'bank': u.reg_read(r.UC_ARM64_REG_W2),
                          'save': u.reg_read(r.UC_ARM64_REG_W5),
                          'context_begin': word(counter), 'ranges': [],
                          '_vector': vector, '_counter': counter,
                          '_offset': end-begin}
            elif pc == 0xcb9a50:
                assert active is not None
                active['selector_register'] = u.reg_read(r.UC_ARM64_REG_W0)
            elif pc == 0xcb9c04:
                assert active is not None and pending is None
                pending = [u.reg_read(getattr(r, f'UC_ARM64_REG_W{i}')) for i in range(3)]
            elif pc == 0xcb9c08:
                assert active is not None and pending is not None
                start, count, save = pending
                encoded = u.reg_read(r.UC_ARM64_REG_X0)
                assert save == active['save'] and encoded == encode_range(start, count, save)
                assert encoded >> 26 == count and (encoded >> 2 & 0x7fffff) == start
                offset = active['context_begin']+sum(x['context_bytes'] for x in active['ranges'])
                active['ranges'].append({'register_begin': start, 'register_end': start+count*4,
                                         'word_count': count, 'context_offset': offset,
                                         'context_bytes': context_bytes(start, count),
                                         'encoded': hex(encoded)})
                pending = None
            else:
                assert pc in (0xcbe788, 0xcbecc8) and active is not None and pending is None
                assert u.reg_read(r.UC_ARM64_REG_W0) == 0
                begin, end, capacity = struct.unpack('<3Q', u.mem_read(active['_vector'], 24))
                assert begin <= begin+active['_offset'] <= end <= capacity
                # The original vector may grow and relocate during generation.
                start = begin+active['_offset']
                assert end-start <= 4096
                data = bytes(u.mem_read(start, end-start))
                expected = b''
                if active['selector_register']:
                    encoded = ((active['selector_register'] | 0x800000) << 2) | active['bank'] << 32
                    expected += struct.pack('<Q', encoded)
                expected += b''.join(struct.pack('<Q', int(x['encoded'], 16)) for x in active['ranges'])
                assert data == expected, (active['engine_id'], active['bank'], active['save'],
                                           data.hex(), expected.hex(), active['selector_register'])
                active['context_end'] = word(active['_counter'])
                assert active['context_end'] == active['context_begin']+sum(x['context_bytes'] for x in active['ranges'])
                for key in ('_vector', '_counter', '_offset'):
                    del active[key]
                groups.append(active)
                active = None

        for pc in (0xcb99f8, 0xcb9a50, 0xcb9c04, 0xcb9c08, 0xcbe788, 0xcbecc8):
            u.hook_add(UC_HOOK_CODE, code, begin=base+pc, end=base+pc)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        collect(['--translation-input'], observer=observer)
    assert json.loads(output.getvalue())['passed']
    assert active is None and pending is None and len(groups) == 134
    saves, restores = groups[:67], groups[67:]
    assert all(g['save'] == 1 for g in saves) and all(g['save'] == 0 for g in restores)
    assert sum(len(g['ranges']) for g in saves) == 137
    for save, restore in zip(saves, restores):
        assert {k: v for k, v in save.items() if k not in ('save', 'ranges')} == {
            k: v for k, v in restore.items() if k not in ('save', 'ranges')}
        assert len(save['ranges']) == len(restore['ranges'])
        for a, b in zip(save['ranges'], restore['ranges']):
            assert {k: v for k, v in a.items() if k != 'encoded'} == {k: v for k, v in b.items() if k != 'encoded'}
            assert int(b['encoded'], 16) == int(a['encoded'], 16) | 1 << 25
    for direction in (saves, restores):
        assert direction[0]['context_begin'] == 0 and direction[-1]['context_end'] == 0x1cb00
        assert all(a['context_end'] == b['context_begin'] for a, b in zip(direction, direction[1:]))
        assert len({g['engine_id'] for g in direction}) == 34
        assert [(g['engine_id'], g['bank']) for g in direction if not g['selector_register']] == [(52, 0)]
    blob = (ROOT/'results/windows-final-backend-output/kind-0.bin').read_bytes()
    saved = struct.unpack('<205Q', blob[0xe00:0xe00+205*8])
    restored = struct.unpack('<206Q', blob[0x4300:0x4300+206*8])
    assert [int(x['encoded'], 16) for g in saves for x in g['ranges']] == [x for x in saved if x & 3 == 2]
    assert tuple(x | 1 << 25 if x & 3 == 2 else x for x in saved) == restored[:-1]
    assert restored[-1] == (1 << 32) | ((0x31000 | 0x800000) << 2)
    return saves


def check_encoders(pe):
    image, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(base, (len(image)+4095) & ~4095)
    u.mem_write(base, image)
    stop = 0x40000000
    u.mem_map(stop, 4096)
    rows = []
    for register in (0, 0x23008, 0x7ffffc):
        for count in (0, 1, 30, 63, 64, 65, 593, 0x7fffffff):
            for save in (0, 1):
                for i, value in enumerate((register, count, save)):
                    u.reg_write(getattr(r, f'UC_ARM64_REG_W{i}'), value)
                u.reg_write(r.UC_ARM64_REG_LR, stop)
                u.emu_start(base+0xcb0860, stop, count=32)
                assert u.reg_read(r.UC_ARM64_REG_PC) == stop
                expected = encode_range(register, count, save)
                assert u.reg_read(r.UC_ARM64_REG_X0) == expected
                rows.append({'register': register, 'count': count, 'save': save, 'encoded': hex(expected)})
    for section in pe.sections:
        if section.Characteristics & 0x20000000:
            assert bytes(u.mem_read(base+section.VirtualAddress, section.SizeOfRawData)) == section.get_data()
    return rows


def main():
    path = ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == COMPILER_HASH
    groups = trace()
    cases = check_encoders(pefile.PE(str(path), fast_load=True))
    print(json.dumps({'passed': True, 'hardware_access': False, 'compiler_sha256': COMPILER_HASH,
                      'groups_per_direction': len(groups), 'ranges_per_direction': 137,
                      'register_context_bytes': 0x1cb00, 'save_groups': groups,
                      'encoder_cases': cases,
                      'limitations': ['Original compiler under modeled Windows/CRT, one captured ReLU graph',
                                      'Bank selector meanings derive from compiler sequencing, not hardware readback',
                                      'Offsets describe compiler accounting; device byte order and padding contents remain unverified',
                                      'The separate 0x280000-byte context component is not decoded here',
                                      'No physical context preservation, DMA-quiescence or safe-reclaim proof']}, indent=2))


if __name__ == '__main__':
    main()
