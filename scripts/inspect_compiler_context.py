#!/usr/bin/env python3
"""Recover compiler context extents and size-to-IL encoding without hardware."""
import contextlib
import hashlib
import io
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_dla_compiler import ROOT
from inspect_windows_mtl_collector import main as collect

COMPILER_HASH = '0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
PARTS = [(0, 0x1cb00), (0x1cb00, 0x280000), (0x29cb00, 0), (0x29cb00, 0)]


def trace_compiler():
    observations = {'parts': [], 'count_instructions': []}
    context = None

    def observer(u, pe, base):
        def code(u, pc, size, user):
            nonlocal context
            pc -= base
            if pc == 0xcbe538:
                assert context is None
                context = u.reg_read(r.UC_ARM64_REG_X0)
                vectors = []
                for reg in (r.UC_ARM64_REG_X2, r.UC_ARM64_REG_X3):
                    begin, end, capacity = struct.unpack('<3Q', u.mem_read(u.reg_read(reg), 24))
                    assert begin <= end <= capacity
                    vectors.append(end-begin)
                observations['extra_vector_bytes'] = vectors
            elif pc in (0xcbe8c0, 0xcbea84, 0xcbec14):
                assert u.reg_read(r.UC_ARM64_REG_X20) == context
                # Original stores have executed. These are offset/size pairs,
                # not inferred from adjacent blob addresses or padding.
                indices = (0, 1) if pc == 0xcbe8c0 else ((2,) if pc == 0xcbea84 else (3,))
                for index in indices:
                    pair = struct.unpack('<II', u.mem_read(context+8+index*8, 8))
                    assert pair == PARTS[index]
                    observations['parts'].append({'index': index, 'offset': pair[0],
                                                   'bytes': pair[1], 'after_store_rva': hex(pc)})
            elif pc == 0xcbab28:
                assert u.reg_read(r.UC_ARM64_REG_X0) == context
                assert u.reg_read(r.UC_ARM64_REG_X5) == context+8
                observations['save_stage1_metadata'] = list(struct.unpack('<8I', u.mem_read(context+8, 32)))
            elif pc == 0x4fcb18:
                register = u.reg_read(r.UC_ARM64_REG_W1)
                if register in (0x25028, 0x2502c):
                    observations['count_instructions'].append({
                        'caller_return_rva': hex(u.reg_read(r.UC_ARM64_REG_LR)-base),
                        'register': hex(register), 'value': u.reg_read(r.UC_ARM64_REG_W2)})
            elif pc == 0xc74dd8:
                assert u.reg_read(r.UC_ARM64_REG_X0)+0x16b0 == context
                assert list(struct.unpack('<8I', u.mem_read(context+8, 32))) == [v for p in PARTS for v in p]
                observations['parts_preserved_until_linking'] = True

        for pc in (0xcbe538, 0xcbe8c0, 0xcbea84, 0xcbec14, 0xcbab28, 0x4fcb18, 0xc74dd8):
            u.hook_add(UC_HOOK_CODE, code, begin=base+pc, end=base+pc)

    # Reuse the actual translation and all existing pinned-output assertions.
    # The observer only reads registers/memory; it changes no compiler state.
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        collect(['--translation-input'], observer=observer)
    original = json.loads(output.getvalue())
    assert original['passed'] and not original['hardware_access']
    assert original['translation']['completed'] and original['translation']['return_value'] == 0
    assert len(observations['parts']) == 4
    assert observations['extra_vector_bytes'] == [0, 0]
    assert observations['parts_preserved_until_linking']
    expected = [(0xcba88c, 0x2502c, 0x72c), (0xcba8d0, 0x25028, 0),
                (0xcbace8, 0x25028, 0), (0xcbad2c, 0x2502c, 0),
                (0xcb9ed4, 0x25028, 0), (0xcb9f18, 0x2502c, 0),
                (0xcba35c, 0x25028, 0x72c), (0xcba3a0, 0x2502c, 0)]
    assert observations['count_instructions'] == [
        {'caller_return_rva': hex(pc), 'register': hex(reg), 'value': value}
        for pc, reg, value in expected]
    observations['original_translation_checks_passed'] = True
    observations['collector_report_sha256'] = hashlib.sha256(output.getvalue().encode()).hexdigest()
    return observations


def check_stage1_builder(pe):
    """Execute the original IL generator with controlled size inputs.

    Synthetic C++ vectors have spare capacity, avoiding allocation/CRT shims.
    The actual generator, IL helpers and prologue/epilogue code all execute.
    """
    image = pe.get_memory_mapped_image()
    base = pe.OPTIONAL_HEADER.ImageBase
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.ctl_set_cpu_model(r.UC_CPU_ARM64_MAX)
    u.mem_map(base, (len(image)+4095) & ~4095)
    u.mem_write(base, image)
    heap, stop = 0x40000000, 0x41000000
    u.mem_map(heap, 0x10000)
    u.mem_map(stop, 4096)
    cases = []
    instructions = (ROOT/'results/windows-final-backend-output/kind-0.bin').read_bytes()
    baseline = instructions[0x6c00:0x6d00]
    for size in (0, 64, 128, 256, 0x1cb00, 1, 63, 65):
        u.mem_write(heap, b'\xa5'*0x10000)
        # Two output vectors: IL starts empty; its slice list already has the
        # previous save-stage record. Its contents are not consumed here.
        u.mem_write(heap, struct.pack('<3Q', heap+0x8000, heap+0x8000, heap+0x9000))
        u.mem_write(heap+0x1000, struct.pack('<3Q', heap+0x9000, heap+0x9010, heap+0xa000))
        u.mem_write(heap+0x2000, struct.pack('<I', 0x1500))
        u.mem_write(heap+0x3000, struct.pack('<I', 0x2c1))
        metadata = [v for p in PARTS for v in p]
        metadata[5] = size
        u.mem_write(heap+0x4000, struct.pack('<8I', *metadata))
        u.mem_write(heap+0x5000, bytes(0x1000))
        u.mem_write(heap+0x5018, struct.pack('<II', 0x6a00, 25))
        u.mem_write(heap+0x6000, struct.pack('<I', 3))
        u.mem_write(heap+0x7000, struct.pack('<I', 5))
        sp = heap+0xf000
        u.mem_write(sp, struct.pack('<Q', heap+0x7000))  # ninth ABI argument
        for i, value in enumerate((0, heap, heap+0x1000, heap+0x2000, heap+0x3000,
                                    heap+0x4000, heap+0x5000, heap+0x6000)):
            u.reg_write(getattr(r, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(r.UC_ARM64_REG_SP, sp)
        u.reg_write(r.UC_ARM64_REG_LR, stop)
        u.emu_start(base+0xcbab28, stop, count=10000)
        assert u.reg_read(r.UC_ARM64_REG_PC) == stop
        status = u.reg_read(r.UC_ARM64_REG_W0)
        start, end, capacity = struct.unpack('<3Q', u.mem_read(heap, 24))
        assert start == heap+0x8000 and capacity == heap+0x9000
        data = bytes(u.mem_read(start, end-start))
        if size % 64:
            assert status == 1 and data == baseline[:64]
            assert struct.unpack('<Q', u.mem_read(heap+0x1008, 8))[0] == heap+0x9010
        else:
            assert status == 0
            expected = bytearray(baseline)
            struct.pack_into('<I', expected, 4*16+8, size//64)
            assert data == expected
            assert bytes(u.mem_read(heap+0x9010, 16)) == struct.pack('<IIHHI', 3, 0x6c00, 9, 16, 0)
            assert bytes(u.mem_read(heap+0x9020, 224)) == bytes(224)
        assert bytes(u.mem_read(end, 16)) == b'\xa5'*16
        cases.append({'context_bytes': size, 'status': status, 'il_bytes': len(data),
                      'count_register': '0x25028',
                      'count_value': None if size % 64 else size//64})
    for section in pe.sections:
        if section.Characteristics & 0x20000000:
            assert bytes(u.mem_read(base+section.VirtualAddress, section.SizeOfRawData)) == section.get_data()
    return cases


def main():
    path = ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == COMPILER_HASH
    observations = trace_compiler()
    cases = check_stage1_builder(pefile.PE(str(path), fast_load=True))
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'compiler_sha256': COMPILER_HASH, 'compiler_trace': observations,
                      'builder_cases': cases,
                      'limitations': ['Original compiler under modeled Windows/CRT, one captured ReLU graph',
                                      'Builder variations use synthetic metadata and preallocated C++ vectors',
                                      'Size units are compiler semantics, not a physical engine DMA measurement',
                                      'No engine arithmetic, context preservation or memory-reclamation proof']}, indent=2))


if __name__ == '__main__':
    main()
