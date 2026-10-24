#!/usr/bin/env python3
"""Validate Linux image staging against the recovered Windows byte/word contract."""
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
from test_sroot_send import ROOT
from extract_firmware import KMD_SHA256


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-sroot-image-') as tmp:
        library = Path(tmp)/'sroot.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_sroot.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    stage = lib.n1x_sroot_stage_image
    stage.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                      ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint32)]

    def check(blob, dma=0x40100000, invalid=False, size=None, capacity=None, inplace=False):
        source = ctypes.create_string_buffer(blob)
        output = source if inplace else ctypes.create_string_buffer(b'\xa5'*len(blob))
        before = output.raw
        words = (ctypes.c_uint32*3)(*[0xa5a5a5a5]*3)
        status = stage(output, len(blob) if capacity is None else capacity, source,
                       len(blob) if size is None else size, dma, words)
        if invalid:
            assert status == -errno.EINVAL and output.raw == before and list(words) == [0xa5a5a5a5]*3
        else:
            assert status == 0
            assert output.raw[:len(blob)] == blob[4:]+bytes(4)
            assert output.raw[len(blob):] == before[len(blob):]
            assert list(words) == [struct.unpack_from('<I', blob)[0], len(blob), dma]
        return status

    cases = 0
    for tag in (0xc0300, 0xc0302, 0xc0303, 0xc0304, 0xc0305, 0x90400):
        for length in (32, 65, 512):
            blob = struct.pack('<I', tag)+bytes((i & 255) for i in range(length-4))
            for inplace in (False, True):
                check(blob, inplace=inplace)
                cases += 1
    blob = struct.pack('<I', 0xc0302)+bytes(28)
    for tag in (0, 0xc0301, 0xc0306, 0xffffffff):
        check(struct.pack('<I', tag)+bytes(28), invalid=True)
    for size in (0, 4, 31, 0x100000000):
        check(blob, size=size, invalid=True)
    check(blob, capacity=31, invalid=True)
    check(blob, dma=0x100000000, invalid=True)
    check(blob, dma=0xffffffe1, invalid=True)
    check(blob, dma=0xffffffe0)  # Last byte exactly 0xffffffff.
    check(blob, dma=0)  # Valid DMA address; not a null CPU pointer.
    words = (ctypes.c_uint32*3)(1, 2, 3)
    buffer = ctypes.create_string_buffer(blob)
    for dst, src, out in ((None, buffer, words), (buffer, None, words), (buffer, buffer, None)):
        assert stage(dst, 32, src, 32, 0x40000000, out) == -errno.EINVAL
    # Two opaque variants actually selected by the P16 HAL's method-2 table.
    import pefile
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    vendor_cases = []
    for rva, length in ((0x1405800, 0x107b4), (0x13e2180, 0x12eb4)):
        image = pe.get_data(rva, length)
        assert len(image) == length
        check(image)
        vendor_cases.append({'rva': hex(rva), 'input_bytes': length,
                             'tag': hex(struct.unpack_from('<I', image)[0])})
    print(json.dumps({'passed': True, 'hardware_access': False, 'synthetic_staging_cases': cases,
                      'vendor_variants': vendor_cases,
                      'limitations': ['Tail zeroing is explicit Linux policy, not observed Windows initialization',
                                      'No signature verification, DMA mapping or firmware submission']}, indent=2))


if __name__ == '__main__':
    main()
