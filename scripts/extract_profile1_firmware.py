#!/usr/bin/env python3
"""Extract the raw firmware parsed by Windows DLA HAL selector 1."""
import argparse
import hashlib
import json
import struct
from pathlib import Path
import pefile
from extract_firmware import KMD_SHA256, parse_header

PROFILE1_HASH = '2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997'


def extract(path, output):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    assert pe.get_data(0xd4842c, 12) == bytes.fromhex('283300900101079183100094')
    rva = 0x13ac1c0
    header = pe.get_data(rva, 24)
    size = struct.unpack_from('<I', header, 8)[0]
    assert size == 66816
    blob = pe.get_data(rva, size)
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    parsed = parse_header(blob)
    assert struct.unpack_from('<I', blob, parsed['os_header_offset'])[0] == 2
    with output.open('xb') as stream:
        stream.write(blob)
    return {'kmd_sha256': KMD_SHA256, 'firmware_sha256': PROFILE1_HASH,
            'hal_selector': 1, 'parser_wrapper_rva': '0xd48410', 'firmware_rva': hex(rva),
            'firmware_file_offset': hex(pe.get_offset_from_rva(rva)), 'header': parsed,
            'inner_version': 2, 'hardware_access': False,
            'scope': 'Raw image selected by the HAL-1 parser; signed boot variants are separate'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kmd', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(extract(args.kmd, args.output), indent=2))
