#!/usr/bin/env python3
"""Compare the captured P16 BIOS-300 ROM with the original Windows parser.

Offline only. Requires the exact private ROM and KMD; never reads hardware.
The initializer's OS/setup boundaries remain explicit stubs in its harness.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

import pefile
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT
from test_hfrp_vbios import Policy, Location, run as parse_windows
from test_hfrp_policy_init import Gating, run as init_windows

ROM_SHA256 = 'a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c'


def values(obj):
    return {name: list(value) if isinstance(value := getattr(obj, name), C.Array) else value
            for name, _ in obj._fields_}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rom', type=Path)
    args = parser.parse_args()
    data = args.rom.read_bytes()
    if len(data) != 0x110000 or hashlib.sha256(data).hexdigest() != ROM_SHA256:
        parser.error('ROM differs from the double-read P16 BIOS-300 capture')
    kmd = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    if hashlib.sha256(kmd.read_bytes()).hexdigest() != KMD_SHA256:
        parser.error('KMD differs from the audited NVIDIA 617.42 binary')
    with tempfile.TemporaryDirectory(prefix='p16-policy-') as tmp:
        library = Path(tmp)/'policy.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_vbios.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
    lib.n1x_dla_parse_vbios_policy.argtypes = [C.POINTER(C.c_uint8), C.c_size_t,
                                             C.POINTER(Policy), C.POINTER(Location)]
    lib.n1x_dla_prepare_gating.argtypes = [C.POINTER(Policy), C.POINTER(Gating)]
    policy, location, gating = Policy(), Location(), Gating()
    buf = (C.c_uint8*len(data)).from_buffer_copy(data)
    assert lib.n1x_dla_parse_vbios_policy(buf, len(data), C.byref(policy), C.byref(location)) == 0
    assert lib.n1x_dla_prepare_gating(C.byref(policy), C.byref(gating)) == 0
    pe = pefile.PE(str(kmd), fast_load=True)
    original = parse_windows(pe, data, instruction_limit=100_000_000)
    assert original['status'] == 0
    fields = original['fields']
    scalar = {'frequency_khz': 0xa8, 'entry_frequency_khz': 0xac,
              'exit_frequency_khz': 0xb0, 'power_cap_word': 0xbc,
              'gating_word': 0xf0, 'gating_flags': 0xec,
              'feature_mask': 0x80, 'general_word': 0x84}
    arrays = {'tuning': 0xc0, 'frequency_header': 0x98, 'tuning_header': 0xb4,
              'gating_header': 0xe4, 'gating_tail': 0x100, 'frequency_flags': 0xa0,
              'gating_bytes': 0xf4, 'general_header': 0x74, 'allocation_words': 0x88}
    for name, offset in scalar.items():
        assert getattr(policy, name) == fields[hex(offset)], name
    for name, offset in arrays.items():
        actual = list(getattr(policy, name))
        assert actual == [fields[hex(offset+4*i)] for i in range(len(actual))], name
    assert policy.boot_tables_valid == 1
    assert location.bit_offset == original['bit_offset']
    assert location.rom_base == original['rom_base']
    assert data[0x28070:0x28078] == bytes.fromhex('50434952de10032e')
    version_text = b'Version 9a.0B.31.00.04'
    assert data[0x27fb5:0x27fb5+len(version_text)] == version_text
    initialized = init_windows(pe, fields)
    assert initialized['status'] == 0
    assert gating.request_mask == initialized['mask']
    assert list(gating.policy_bytes) == initialized['gating_bytes']
    assert gating.slcg_override == initialized['slcg_override']
    assert list(gating.hal_flags) == list(initialized['hal_flags'].values())
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'rom_sha256': ROM_SHA256, 'kmd_sha256': KMD_SHA256,
                      'bytes': len(data), 'policy': values(policy), 'location': values(location),
                      'pcir_vendor_device': '10de:2e03', 'vbios_version': '9a.0B.31.00.04',
                      'gating': values(gating), 'windows_initializer': initialized,
                      'limitation': 'Original parser/initializer replay with OS/setup stubs; no physical boot'},
                     indent=2))


if __name__ == '__main__':
    main()
