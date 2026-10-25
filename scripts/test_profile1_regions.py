#!/usr/bin/env python3
"""Windows/C region records through original HAL-1 reset/trap/DMA/ACK code."""
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
import pefile
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH
from emulate_windows_region import build
from extract_firmware import KMD_SHA256


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    with tempfile.TemporaryDirectory(prefix='n1x-hal1-regions-') as tmp:
        library = Path(tmp)/'regions.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_command.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        serialize = lib.n1x_dla_build_region
        serialize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                              ctypes.c_uint64, ctypes.c_uint32]
        for region, offset, size, header in (
                (1, 256, 256, (0, 0)), (3, 256, 256, (0, 0)),
                (5, 256, 256, (0, 0)), (5, 512, 1024, (8, 8)),
                (5, 256, 512, (64, 128))):
            record = ctypes.create_string_buffer(16)
            assert serialize(record, 16, region, HOST+offset, size) == 0
            windows = build(pe, region, dma=HOST, offset=offset, size=size)
            assert windows['status'] == 0 and bytes(record).hex() == windows['record']
            assert windows['methods'] == [{'command': 11, 'data': HOST >> 8, 'argument_5': 0}]
            payload = bytearray(offset+size)
            payload[:16] = record.raw
            payload[offset:offset+8] = struct.pack('<II', *header)
            observed = []

            def response(u):
                if region == 5:
                    observed.append({'descriptor': bytes(u.mem_read(0x1859d0, 16)).hex(),
                                     'header': struct.unpack('<II', u.mem_read(0x1860f8, 8))})
                u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

            result = run(blob, method=0x30b, boot_from_reset=True, via_trap=True,
                         dma_register_model=True, host_payload=payload, host_handler=response)
            assert result['returned'] and result['firmware_code_unmodified']
            assert result['function_stubs'] == []
            assert result['interrupts'] == [(0xb02, HOST >> 8)]
            assert bytes.fromhex(result['host_payload_final']) == payload
            if region == 5:
                assert observed == [{'descriptor': record.raw.hex(),
                                     'header': (8, 8) if header == (0, 0) else header}]
                assert [d['bytes'] for d in result['dma_commands']] == [4, 16, 8]
                assert result['dma_commands'][-1]['source'] == HOST+offset
            rows.append({'region': region, 'offset': offset, 'bytes': size,
                         'input_header': header, 'firmware_state': observed,
                         'response': result['interrupts'], 'dma': result['dma_commands']})
        for region, size in ((5, 0), (5, 7), (7, 256)):
            record = ctypes.create_string_buffer(b'Z'*16, 16)
            assert serialize(record, 16, region, HOST+256, size) == -errno.EINVAL
            assert record.raw == b'Z'*16
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'limitations': ['Windows allocation/cache/send boundaries are synthetic',
                                      'RV32 firmware code is unchanged; DMA/IRQ/reset device effects are modeled',
                                      'Registration and local ring-header state only; no ring production/consumption or inference',
                                      'Region 7 dispatch exists in HAL-1 but its format is not covered here']}, indent=2))


if __name__ == '__main__':
    main()
