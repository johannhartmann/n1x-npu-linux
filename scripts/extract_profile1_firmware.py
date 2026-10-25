#!/usr/bin/env python3
"""Extract HAL-1 raw research firmware or an opaque SROOT upload variant."""
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
            'scope': 'Raw image selected by the HAL-1 parser; opaque boot variants are separate'}


def extract_boot_variant(path, output, remapper=1, variant=0):
    if remapper not in (0, 1, 2) or variant not in (0, 1):
        raise ValueError('Only verified P16 remapper modes 0/1/2 and variants 0/1 are supported')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    record = 0x108fc70
    assert struct.unpack('<I', pe.get_data(record, 4))[0] == 0x254
    group = int(remapper != 1)
    pointer, size = struct.unpack('<QI', pe.get_data(record+8+(group*3+variant)*16, 12))
    rva = pointer-pe.OPTIONAL_HEADER.ImageBase
    expected = {
        (0, 0): (0x1405800, 67508, '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4'),
        (0, 1): (0x13f5040, 67508, '6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4'),
        (1, 0): (0x13e2180, 77492, 'a35d5fa8e06a86ed17d796027a6d7dea1d04b728ddd349e9d7d1e6d0e8892ccb'),
        (1, 1): (0x13cf2c0, 77492, 'b36129012b36ec7a3096371b0d049b1a42e5be4eff1dcdc3f4317bb113fda647')}
    blob = pe.get_data(rva, size)
    digest = hashlib.sha256(blob).hexdigest()
    assert (rva, size, digest) == expected[group, variant]
    assert len(blob) == size and struct.unpack_from('<I', blob)[0] == 0xc0302
    with output.open('xb') as stream:
        stream.write(blob)
    return {'kmd_sha256': KMD_SHA256, 'firmware_sha256': digest,
            'device_tag': '0x254', 'remapper': remapper, 'variant': variant,
            'firmware_rva': hex(rva), 'bytes': size, 'hardware_access': False,
            'scope': 'Opaque SROOT upload; authentication and hardware acceptance are unverified'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kmd', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--sroot-variant', type=int, choices=(0, 1),
                        help='Extract an opaque upload instead of the raw research image')
    parser.add_argument('--remapper', type=int, choices=(0, 1, 2),
                        help='SROOT remapper mode; defaults to constructor value 1')
    args = parser.parse_args()
    if args.sroot_variant is None:
        if args.remapper is not None:
            parser.error('--remapper requires --sroot-variant')
        result = extract(args.kmd, args.output)
    else:
        result = extract_boot_variant(args.kmd, args.output,
                                      1 if args.remapper is None else args.remapper,
                                      args.sroot_variant)
    print(json.dumps(result, indent=2))
