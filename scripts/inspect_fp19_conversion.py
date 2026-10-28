#!/usr/bin/env python3
"""Execute original Windows/native compiler FP19 converters in isolation.

Only original leaf-function code pages and a return sentinel are mapped.
No helper replacements, library initialization, device access or inference.
"""
import hashlib
import json
import random
import struct

import pefile
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
from unicorn import arm64_const as r

from inspect_dla_compiler import ROOT, NATIVE_HASH

WINDOWS_HASH = '0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
STOP = 0x71000000


def encode(bits):
    sign, exponent, fraction = bits >> 31, (bits >> 23) & 255, bits & 0x7fffff
    if not exponent:
        return sign << 18  # Original encoder flushes FP32 subnormals.
    if exponent == 255:
        return 0x3ffff if fraction else (sign << 18) | 0x3fbff
    retained, remainder = divmod(bits & 0x7fffffff, 1 << 13)
    if remainder > 4096 or remainder == 4096 and retained & 1:
        retained += 1
    return (sign << 18) | min(retained, 0x3fbff)


def decode(word):
    word &= 0x7ffff
    if (word >> 10) & 255 == 255 and word & 1023:
        return 0x7f8003ff  # Observed canonical NaN payload, not a shift.
    return word << 13


def machine(page, data, encoder, decoder):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(page, 4096)
    u.mem_write(page, data)
    u.mem_map(STOP, 4096)
    def call(value, forward):
        u.reg_write(r.UC_ARM64_REG_X30, STOP)
        u.reg_write(r.UC_ARM64_REG_S0 if forward else r.UC_ARM64_REG_W0, value)
        u.emu_start(encoder if forward else decoder, STOP, count=1000)
        assert u.reg_read(r.UC_ARM64_REG_PC) == STOP
        return u.reg_read(r.UC_ARM64_REG_W0 if forward else r.UC_ARM64_REG_S0)
    return call


def main():
    windows = ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    native = ROOT/'sources/tensorrt-11.4/native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so'
    assert hashlib.sha256(windows.read_bytes()).hexdigest() == WINDOWS_HASH
    assert hashlib.sha256(native.read_bytes()).hexdigest() == NATIVE_HASH
    pe = pefile.PE(str(windows))
    base = pe.OPTIONAL_HEADER.ImageBase
    win = machine(base+0x3d9000, pe.get_data(0x3d9000, 4096), base+0x3d9420, base+0x3d93b8)
    with native.open('rb') as f:
        elf = ELFFile(f)
        segment = next(s for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD'
                       and s['p_vaddr'] <= 0x4e1000 and s['p_vaddr']+s['p_filesz'] >= 0x4e2000)
        offset = 0x4e1000-segment['p_vaddr']
        linux = machine(0x4e1000, segment.data()[offset:offset+4096], 0x4e1d80, 0x4e1d20)
    rng = random.Random(19)
    boundaries = [0, 1, 0x7fffff, 0x800000, 0x3f800000, 0x3f800fff,
                  0x3f801000, 0x3f801001, 0x3f803000, 0x3ffff000,
                  0x7f7fefff, 0x7f7ff000, 0x7f7fffff, 0x7f800000,
                  0x7f800001, 0x7fc00000]
    values = [v | sign for sign in (0, 1 << 31) for v in boundaries]
    values += [rng.getrandbits(32) for _ in range(1024)]
    for value in values:
        assert win(value, True) == linux(value, True) == encode(value), hex(value)
    words = [0, 1, 0x3ff, 0x400, 0x1fc00, 0x3fbff, 0x3fc00, 0x3ffff]
    words = [w | sign for sign in (0, 1 << 18) for w in words]
    words += [rng.getrandbits(19) for _ in range(1024)]
    for word in words:
        assert win(word, False) == linux(word, False) == decode(word), hex(word)
    bias = (ROOT/'results/native-compile-t254-conv1x1/constant-8960.bin').read_bytes()
    assert len(bias) == 128
    for c, word in enumerate(struct.unpack('<32I', bias)):
        original = struct.unpack('<I', struct.pack('<f', c/32-0.5))[0]
        assert win(original, True) == linux(original, True) == word
        assert win(word, False) == linux(word, False) == original
    print(json.dumps(dict(passed=True, hardware_access=False,
        windows_sha256=WINDOWS_HASH, native_sha256=NATIVE_HASH,
        windows_rvas=dict(encode='0x3d9420', decode='0x3d93b8'),
        native_offsets=dict(encode='0x4e1d80', decode='0x4e1d20'),
        encode_cases=len(values), decode_cases=len(words), exact_fixture_biases=32,
        helpers_modeled=False, instructions_unmodified=True,
        limitations=['Compiler software conversion, not arithmetic-engine behavior',
                     'Fixture constants agree; this does not trace their complete producing call chain']), indent=2))


if __name__ == '__main__':
    main()
