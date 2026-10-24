#!/usr/bin/env python3
"""Verify setup ABI differences using the actual T35x dispatcher in RAM."""
import hashlib
import json
import struct
from emulate_firmware_commands import ROOT, HOST, BLOB_HASH, run


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    rows = []
    for region in (0, 1, 2, 3, 4, 5, 6, 7):
        result = run(blob, method=0x30b, payload=struct.pack('<IQI', region, 0, 0))
        accepted = region in (1, 2, 3, 6)
        assert result['returned']
        assert result['interrupts'] == [(0xb02 if accepted else 0xb0b01, HOST >> 8)]
        assert [t['length'] for t in result['transfers']] == ([4, 56 if region == 2 else 16] if accepted else [4])
        rows.append({'case': f'region_{region}_zero_address_and_size', **result})
    payload = struct.pack('<IQI', 1, 0x1234567890, 512)
    result = run(blob, method=0x30b, payload=payload, observe=((0x1889b0, 16),))
    assert result['returned'] and result['interrupts'] == [(0xb02, HOST >> 8)]
    assert result['observed']['0x1889b0'] == payload.hex()
    rows.append({'case': 'printf_region_wire_fields_preserved', **result})
    for subcommand in (0, 1, 2, 3):
        result = run(blob, method=0x30a, payload=struct.pack('<IQQ', subcommand, 0x123456789abcdef0, 0),
                     observe=((0x18b200, 8),))
        expected = struct.pack('<II', 0x9abcdef0 if subcommand == 1 else 0,
                               0x9abcdef0 if subcommand == 2 else 0)
        assert result['returned']
        assert result['interrupts'] == [(0xa02 if subcommand in (1, 2) else 0x30a01, HOST >> 8)]
        assert result['observed']['0x18b200'] == expected.hex()
        assert [t['length'] for t in result['transfers']] == [20]
        rows.append({'case': f'debug_subcommand_{subcommand}_low_u32', **result})
    for command, payload in ((10, bytes(20)), (11, bytes(16))):
        result = run(blob, method=0x300 | command, payload=payload, dma_error='host_to_firmware')
        assert result['returned'] and result['interrupts'] == [(0x40001 | command << 8, HOST >> 8)]
        assert len(result['transfers']) == 1
        rows.append({'case': f'command_{command}_dma_failure', **result})
    # Original state getter reads 0x18930c. States 2..4 reject preemption
    # before it touches the execution engine or decodes a task payload.
    for state in (2, 3, 4):
        result = run(blob, method=0x311, payload=bytes(16),
                     memory={0x18930c: struct.pack('<I', state)})
        assert result['returned'] and not result['transfers']
        assert result['interrupts'] == [(0xc1101, HOST >> 8)]
        rows.append({'case': f'command_17_preemption_rejected_in_state_{state}', **result})
    print(json.dumps({'firmware_sha256': BLOB_HASH, 'passed': True, 'hardware_access': False,
                      'scope': 'Original dispatcher and setup handlers with RAM DMA and host ACK',
                      'cases': rows,
                      'limitations': ['Region zero-address acceptance does not prove a usable configured region',
                                      'Trace-ring payload handling and the meaning of region 6 remain incomplete',
                                      'Preemption execution in allowed states is not exercised',
                                      'No physical memory, firmware boot or inference was tested']}, indent=2))


if __name__ == '__main__':
    main()
