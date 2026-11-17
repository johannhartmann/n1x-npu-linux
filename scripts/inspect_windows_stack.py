#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inventory hash-pinned Windows DLA userspace and related platform drivers.

Static PE inspection only; it does not execute vendor code or use hardware.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

import pefile

from prepare_inputs import MANUAL, ROOT

WINDOWS = {
    'nvcudla.dll': '8da1b42d35aa79c71e3102f95dde30a75dbe498983d386b3cb68082f6b467ba0',
    'nvdla_runtime.dll': 'c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b',
}
PLATFORM = {
    'nvpep.sys': ('nvpep.inf_*', '34cf1f2a9372a092555fb0887bfb76a8f205a76ff7e557224b353b09497c9bb1'),
    'nvSCP.sys': ('nvscp.inf_*', 'd1cc1ae1a907d607ad979e84116a5febdb2f55136b612b210c68fdd57f901a47'),
    'nvsrootmbx.sys': ('nvsrootmbx.inf_*', 'f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a'),
}
INTERESTING = (b'D3DKMTSubmitCommand', b'D3DKMTEscape', b'D3DKMTCreateAllocation',
               b'NvDlaKmdSubmit', b'HFRP is busy')
FIRMWARE_POINTERS = (0x108d878, 0x108d888, 0x108d8a8, 0x108d8b8,
                     0x108d8e0, 0x108d8f0, 0x108d900, 0x108d910,
                     0x108d920, 0x108d930)


def firmware_table(path):
    raw = path.read_bytes()
    image = pefile.PE(data=raw, fast_load=True)
    if struct.unpack_from('<I', raw, 0x108d870)[0] != 0x254 or \
            struct.unpack_from('<I', raw, 0x108d8d8)[0] != 0x256:
        raise ValueError('unexpected KMD firmware table markers')
    rows = []
    for offset in FIRMWARE_POINTERS:
        address, size = struct.unpack_from('<QQ', raw, offset)
        rva = address - image.OPTIONAL_HEADER.ImageBase
        envelope = image.get_data(rva, size)
        if size not in (67508, 77492) or len(envelope) != size or \
                not envelope.startswith(b'\x02\x03\x0c\x00'):
            raise ValueError(f'invalid KMD firmware table row at {offset:#x}')
        rows.append({'table_offset': hex(offset), 'image_rva': hex(rva),
                     'bytes': size, 'sha256': hashlib.sha256(envelope).hexdigest()})
    if len({row['image_rva'] for row in rows}) != 10:
        raise ValueError('duplicate KMD firmware table address')
    return rows


def inspect(path, expected):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected:
        raise ValueError(f'{path.name}: unexpected SHA-256 {digest}')
    image = pefile.PE(data=raw, fast_load=True)
    if image.FILE_HEADER.Machine != 0xaa64:
        raise ValueError(f'{path.name}: expected ARM64 PE')
    image.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
        pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT'],
    ])
    imports = {
        entry.dll.decode(errors='replace'): sorted(
            item.name.decode(errors='replace') if item.name else f'ordinal:{item.ordinal}'
            for item in entry.imports)
        for entry in getattr(image, 'DIRECTORY_ENTRY_IMPORT', [])
    }
    exports = sorted(symbol.name.decode(errors='replace')
                     for symbol in getattr(getattr(image, 'DIRECTORY_ENTRY_EXPORT', None),
                                           'symbols', []) if symbol.name)
    return {
        'sha256': digest,
        'imports': imports,
        'dla_exports': [name for name in exports if 'Dla' in name or 'cudla' in name],
        'ascii_strings': {needle.decode(): raw.count(needle) for needle in INTERESTING},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--display-driver', type=Path,
                        default=ROOT / 'sources/nvidia/Display.Driver')
    parser.add_argument('--platform-driverstore', type=Path,
                        help='Optional extracted Windows/System32/DriverStore/FileRepository')
    args = parser.parse_args()
    results = {'hardware_access': False, 'display_driver': {}, 'platform': {}}
    for name, digest in ((Path(MANUAL[0][0]).name, MANUAL[0][1]), *WINDOWS.items()):
        results['display_driver'][name] = inspect(args.display_driver / name, digest)
    results['firmware_table'] = firmware_table(args.display_driver / Path(MANUAL[0][0]).name)
    if args.platform_driverstore:
        for name, (folder, digest) in PLATFORM.items():
            matches = sorted(args.platform_driverstore.glob(f'{folder}/{name}'))
            if len(matches) != 1:
                raise ValueError(f'{name}: expected one matching driver, found {len(matches)}')
            results['platform'][name] = inspect(matches[0], digest)
    frontend = results['display_driver']['nvcudla.dll']
    if 'cuGetExportTable' not in frontend['imports'].get('nvcuda.dll', []):
        raise ValueError('nvcudla.dll: expected CUDA export-table import')
    if 'GetProcAddress' not in frontend['imports'].get('KERNEL32.dll', []):
        raise ValueError('nvcudla.dll: expected dynamic symbol lookup')
    if frontend['ascii_strings']['D3DKMTSubmitCommand'] < 1:
        raise ValueError('nvcudla.dll: D3DKMT submission name missing')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
