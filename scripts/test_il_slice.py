#!/usr/bin/env python3
"""Check host IL bounds against original firmware DMA extents and diagnostic data."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import struct
import subprocess
import tempfile
from pathlib import Path
from emulate_il_reads import ROOT, BLOB_HASH, run
from emulate_profile1_il_reads import run as run_profile1
from extract_profile1_firmware import PROFILE1_HASH
from inspect_loadable import Buffer


class Range(ctypes.Structure):
    _fields_ = [('address', ctypes.c_uint64), ('bytes', ctypes.c_uint64)]


class Result(ctypes.Structure):
    _fields_ = [('address', ctypes.c_uint64), ('dma_bytes', ctypes.c_uint32), ('instructions', ctypes.c_uint16)]


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    profile1 = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(profile1).hexdigest() == PROFILE1_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-dla-il-') as tmp:
        library = Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_task.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    validate = lib.n1x_dla_validate_il_slice
    validate.argtypes = [ctypes.POINTER(Result), ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.POINTER(Range), ctypes.c_uint16]
    for count in (1, 3, 15, 16, 17, 31, 32, 33, 48, 64):
        observed = run(blob, count)
        extent = observed['dma_extent']
        assert run_profile1(profile1, count) == observed
        source = struct.pack('<IIHH', 0, 256, count, 16)
        buffers = (Range*1)(Range(0x30000000, 256+extent))
        output = Result()
        assert validate(ctypes.byref(output), source, 12, buffers, 1) == 0
        assert output.address == 0x30000100 and output.dma_bytes == extent and output.instructions == count
        buffers[0].bytes -= 1
        ctypes.memset(ctypes.byref(output), 0xa5, ctypes.sizeof(output))
        before = bytes(output)
        assert validate(ctypes.byref(output), source, 12, buffers, 1) == -errno.EINVAL
        assert bytes(output) == before
    buffers = (Range*1)(Range(0x30000000, 4096))
    for index, offset, count in ((1, 0, 1), (0xffffffff, 0, 1), (0x10000, 0, 1), (0, 0, 0),
                                  (0, 1, 1), (0, 4096, 1), (0, 0xffffffff, 1)):
        output = Result()
        ctypes.memset(ctypes.byref(output), 0xa5, ctypes.sizeof(output))
        before = bytes(output)
        assert validate(ctypes.byref(output), struct.pack('<IIHH', index, offset, count, 0), 12, buffers, 1) == -errno.EINVAL
        assert bytes(output) == before
    # Extract only the observed init-IL field at network +20. This is not a
    # complete modern loadable-schema or instruction validator.
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == '51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543'
    b = Buffer(data)
    blobs = {}
    for table in b.tables(b.u('<I', 0), 5):
        vector = b.vector(table, 5, 1)
        blobs[b.string(table, 0)] = data[vector.start:vector.stop]
    source = blobs['task0-addr0'][20:32]
    assert struct.unpack('<IIHH', source) == (1, 122880, 3, 16)
    buffers = (Range*2)(Range(0x30000000, 256), Range(0x31000000, len(blobs['task0-ib'])))
    output = Result()
    assert validate(ctypes.byref(output), source, 12, buffers, 2) == 0
    assert output.address == 0x3101e000 and output.dma_bytes == 256 and output.instructions == 3
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'firmware_extent_comparisons': 10, 'profile1_extent_comparisons': 10, 'short_buffer_rejections': 10,
                      'malformed_slice_rejections': 7,
                      'diagnostic_init_slice': {'index': 1, 'offset': 122880, 'instructions': 3, 'dma_bytes': 256},
                      'limitations': ['Only one known network field is validated',
                                      'Instruction semantics, embedded references and inference are not validated']}, indent=2))


if __name__ == '__main__':
    main()
