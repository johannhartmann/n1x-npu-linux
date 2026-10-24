#!/usr/bin/env python3
"""Feed the C address-list serializer output to the original firmware reader."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from emulate_address_list import ROOT, BLOB_HASH, run
from emulate_profile1_address_list import run as run_profile1
from extract_profile1_firmware import PROFILE1_HASH


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    profile1 = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(profile1).hexdigest() == PROFILE1_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-dla-address-') as tmp:
        path = Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_task.c'), '-o', str(path)], check=True)
        lib = ctypes.CDLL(str(path))
    size = lib.n1x_dla_address_list_bytes
    size.argtypes, size.restype = [ctypes.c_uint16], ctypes.c_size_t
    build = lib.n1x_dla_build_address_list
    build.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint16]
    for count in (1, 9, 32, 33, 511, 512):
        addresses = [0x123456780000+i*256 for i in range(count)]
        source = (ctypes.c_uint64*count)(*addresses)
        expected_size = (count*8+255)&~255
        assert size(count) == expected_size
        output = ctypes.create_string_buffer(b'\xa5'*(expected_size+8), expected_size+8)
        assert build(output, expected_size, source, count) == 0
        assert output.raw[count*8:expected_size] == bytes(expected_size-count*8)
        assert output.raw[expected_size:] == b'\xa5'*8
        result = run(blob, addresses, (0, count-1), raw_list=output.raw[:expected_size])
        assert result['status'] == 0
        for item in result['lookups']:
            assert item['status'] == 0 and item['value'] == addresses[item['index']]
        target = run_profile1(profile1, addresses, (0, count-1), raw_list=output.raw[:expected_size])
        assert target['status'] == 0 and target['transfers'][0]['bytes'] == count*8
        for item in target['lookups']:
            assert item['status'] == 0 and item['value'] == addresses[item['index']]
    source = (ctypes.c_uint64*1)(0)
    for count, length, addr in ((0, 256, source), (513, 8192, source),
                               (1, 255, source), (1, 256, None)):
        output = ctypes.create_string_buffer(b'\xa5'*256, 256)
        assert build(output, length, addr, count) == -errno.EINVAL
        assert output.raw == b'\xa5'*256
    assert size(0) == 0 and size(513) == 0
    assert build(None, 256, source, 1) == -errno.EINVAL
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'scope': 'C output through original firmware cache loader and lookup',
                      'firmware_roundtrip_cases': 6, 'profile1_roundtrip_cases': 6, 'preflight_failures': 5,
                      'limitations': ['Larger paged lists, model index validation and DMA ownership remain open',
                                      'No task execution or numerical inference was performed']}, indent=2))


if __name__ == '__main__':
    main()
