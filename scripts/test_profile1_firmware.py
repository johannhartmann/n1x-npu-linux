#!/usr/bin/env python3
"""Compare Linux and original Windows parsing of the HAL-1 firmware image."""
import ctypes
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from emulate_firmware_commands import ROOT
from emulate_firmware_parser import run, BASE
from extract_profile1_firmware import PROFILE1_HASH


class Header(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'payload_offset', 'payload_size', 'monitor_code_offset', 'monitor_code_size',
        'monitor_data_offset', 'monitor_data_size')]


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    windows = run(blob)
    assert windows == {'status': 0, 'payload_pointer': hex(BASE+512),
                       'decoded_words': [0, 0, 0, 36864, 36864, 29440]}
    with tempfile.TemporaryDirectory(prefix='n1x-profile1-fw-') as tmp:
        library = Path(tmp)/'firmware.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_firmware.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    parse = lib.n1x_dla_parse_firmware
    parse.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Header)]
    header = Header()
    assert parse(blob, len(blob), ctypes.byref(header)) == 0
    assert tuple(getattr(header, name) for name, _ in Header._fields_) == (512, 66304, 0, 36864, 36864, 29440)
    print(json.dumps({'passed': True, 'firmware_sha256': PROFILE1_HASH, 'hardware_access': False,
                      'windows': windows, 'linux': {name: getattr(header, name) for name, _ in Header._fields_},
                      'limitations': ['Parsing does not verify signed boot selection or firmware execution',
                                      'Prior T35x protocol tests do not validate this different image']}, indent=2))


if __name__ == '__main__':
    main()
