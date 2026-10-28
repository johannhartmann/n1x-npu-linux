#!/usr/bin/env python3
"""Trace the T254 compiler's second context component and indirect addresses."""
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


def encode_address(register, slot, offset, selector):
    assert 0 <= register < 1 << 23 and 0 <= slot < 64
    assert 0 <= offset < 1 << 32 and offset % 8 == 0 and selector in (0, 1)
    return register << 2 | slot << 26 | 0x02000001 | ((offset//8)*2 | selector) << 32


def trace():
    records, active, pending, target = [], None, None, None

    def observer(u, pe, base):
        def word(address):
            return int.from_bytes(u.mem_read(address, 4), 'little')

        def code(u, pc, size, user):
            nonlocal active, pending, target
            pc -= base
            if pc == 0xcbe8b0:
                address = u.reg_read(r.UC_ARM64_REG_X0)
                vtable = int.from_bytes(u.mem_read(address, 8), 'little')-base
                assert vtable == 0x11a5f50
                locator = struct.unpack('<Q', pe.get_data(vtable-8, 8))[0]-base
                type_rva = struct.unpack('<6I', pe.get_data(locator, 24))[3]
                name = pe.get_data(type_rva+16, 128).split(b'\0')[0].decode()
                assert name == '.?AVT254DeviceInfo@@'
                target = {'rtti': name, 'capacity_field': '0x28', 'context_bytes': word(address+0x28)}
                assert target['context_bytes'] == 0x280000
            elif pc in (0xcbc6e0, 0xcbb0c0):
                assert active is None and pending is None
                args = [u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}')) for i in range(8)]
                begin, end, capacity = struct.unpack('<3Q', u.mem_read(args[1], 24))
                assert begin <= end <= capacity and end-begin == 16
                extra_begin, extra_end, _ = struct.unpack('<3Q', u.mem_read(args[2], 24))
                assert extra_begin == extra_end
                active = {'phase': 'save' if pc == 0xcbc6e0 else 'restore',
                          'builder_rva': hex(pc), 'bytes': word(args[3]),
                          'context_offset': word(args[4]), 'address_slot': word(args[5]),
                          'words': [], 'addresses': [], '_vector': args[1]}
                assert (active['bytes'], active['context_offset'], active['address_slot']) == (0x280000, 0x1cb00, 5)
            elif pc in (0xcb0830, 0xcb0808) and active is not None:
                assert pending is None
                args = [u.reg_read(getattr(r, f'UC_ARM64_REG_W{i}')) for i in range(4)]
                if pc == 0xcb0830:
                    register, value, mode = args[:3]
                    assert mode == 0 and register < 1 << 23
                    expected = ((register | 0x800000) << 2) | value << 32
                    pending = (0xcb084c, expected)
                else:
                    register, slot, offset8, selector = args
                    assert slot == active['address_slot'] and offset8*8 == active['context_offset']
                    expected = encode_address(register, slot, offset8*8, selector)
                    active['addresses'].append({'register': register, 'slot': slot,
                                                 'offset': offset8*8, 'selector': selector,
                                                 'encoded': hex(expected)})
                    pending = (0xcb082c, expected)
            elif pc in (0xcb084c, 0xcb082c) and active is not None:
                assert pending is not None and pending[0] == pc
                value = u.reg_read(r.UC_ARM64_REG_X0)
                assert value == pending[1]
                active['words'].append(value)
                pending = None
            elif pc in (0xcbe96c, 0xcbeaf0):
                assert active is not None and pending is None
                assert u.reg_read(r.UC_ARM64_REG_W0) == 0
                begin, end, capacity = struct.unpack('<3Q', u.mem_read(active['_vector'], 24))
                assert end-begin == 705*8 and end <= capacity
                data = bytes(u.mem_read(begin, end-begin))
                prefix = struct.pack('<QQ', (0x18 << 32) | ((0x3018 | 0x800000) << 2),
                                      (0x3014 | 0x800000) << 2)
                assert data == prefix+b''.join(struct.pack('<Q', w) for w in active['words'])
                active['program_sha256'] = hashlib.sha256(data).hexdigest()
                active['_data'] = data
                del active['_vector']
                records.append(active)
                active = None

        for pc in (0xcbe8b0, 0xcbc6e0, 0xcbb0c0, 0xcb0830, 0xcb0808,
                   0xcb084c, 0xcb082c, 0xcbe96c, 0xcbeaf0):
            u.hook_add(UC_HOOK_CODE, code, begin=base+pc, end=base+pc)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        collect(['--translation-input'], observer=observer)
    assert json.loads(output.getvalue())['passed'] and active is None and pending is None
    assert target is not None and len(records) == 2
    blob = (ROOT/'results/windows-final-backend-output/kind-0.bin').read_bytes()
    for record, offset, pair, size_register in zip(records, (0x1500, 0x2c00),
                                                   ((0x238ac, 0x238b0), (0x238e4, 0x238e8)),
                                                   (0x238fc, 0x2391c)):
        assert record.pop('_data') == blob[offset:offset+705*8]
        assert [(a['register'], a['selector']) for a in record['addresses']] == [(pair[0], 1), (pair[1], 0)]
        words = record.pop('words')
        assert len(words) == 703 and sum(w & 3 == 1 for w in words) == 2
        writes = [(w >> 2 & 0x7fffff, w >> 32) for w in words if w & 3 == 0]
        assert (size_register, 0x280000) in writes
        assert writes[-1] == (0x23080, 1)
        record['program_offset'] = offset
        record['program_words'] = 705
        record['size_register'] = hex(size_register)
        record['nonzero_writes'] = [[hex(a), hex(v)] for a, v in writes if v]
    return target, records


def check_encoder(pe):
    image, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(base, (len(image)+4095) & ~4095)
    u.mem_write(base, image)
    stop = 0x40000000
    u.mem_map(stop, 4096)
    cases = []
    for slot in (0, 5, 63):
        for offset in (0, 8, 0x1cb00, 0xfffffff8):
            for selector in (0, 1):
                register = 0x238ac if selector else 0x238b0
                for i, value in enumerate((register, slot, offset//8, selector)):
                    u.reg_write(getattr(r, f'UC_ARM64_REG_W{i}'), value)
                u.reg_write(r.UC_ARM64_REG_LR, stop)
                u.emu_start(base+0xcb0808, stop, count=32)
                assert u.reg_read(r.UC_ARM64_REG_PC) == stop
                expected = encode_address(register, slot, offset, selector)
                assert u.reg_read(r.UC_ARM64_REG_X0) == expected
                cases.append({'slot': slot, 'offset': offset, 'selector': selector, 'encoded': hex(expected)})
    for section in pe.sections:
        if section.Characteristics & 0x20000000:
            assert bytes(u.mem_read(base+section.VirtualAddress, section.SizeOfRawData)) == section.get_data()
    return cases


def main():
    path = ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == COMPILER_HASH
    target, programs = trace()
    cases = check_encoder(pefile.PE(str(path), fast_load=True))
    print(json.dumps({'passed': True, 'hardware_access': False, 'compiler_sha256': COMPILER_HASH,
                      'target': target, 'programs': programs, 'encoder_cases': cases,
                      'context_interval': [0x1cb00, 0x29cb00],
                      'limitations': ['Original compiler under modeled Windows/CRT for one ReLU graph',
                                      'T254DeviceInfo is compiler configuration, not a physical capacity measurement',
                                      'Indirect selector fields are encoded as observed; physical split-address interpretation is unverified',
                                      'No engine-side copy simulation, SRAM identity, data preservation or DMA-quiescence proof']}, indent=2))


if __name__ == '__main__':
    main()
