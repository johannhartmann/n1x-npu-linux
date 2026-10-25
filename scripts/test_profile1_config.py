#!/usr/bin/env python3
"""Verify raw HAL-1 config export/import through original reset/trap/DMA code."""
import hashlib
import json
import struct
from emulate_profile1_commands import run, ROOT, HOST, MMIO, ACK, PROFILE1_HASH

CONFIG = 0x186154
SIZE = 252


def execute(blob, command, payload):
    observed = []

    def response(u):
        observed.append(bytes(u.mem_read(CONFIG, SIZE)))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, method=0x300 | command, boot_from_reset=True, via_trap=True,
                 dma_register_model=True, host_payload=payload, host_handler=response)
    assert result['returned'] and result['firmware_code_unmodified']
    assert result['function_stubs'] == []
    assert result['interrupts'] == [(command << 8 | 2, HOST >> 8)]
    direction = 'write' if command == 22 else 'read'
    commands = result['dma_commands']
    assert len(commands) == 63
    for index, dma in enumerate(commands):
        assert dma['bytes'] == 4 and dma['direction'] == direction
        assert dma['source'] == (CONFIG if command == 22 else HOST)+4*index
        assert dma['destination'] == (HOST if command == 22 else CONFIG)+4*index
    assert len(observed) == 1
    final = bytes.fromhex(result['host_payload_final'])
    assert final[SIZE:] == payload[SIZE:]
    if command == 21:
        assert final == payload
    return observed[0], final, result


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    initial = b'\xa5'*4096
    factory, exported, result = execute(blob, 22, initial)
    assert exported[:SIZE] == factory and factory[0] == 2
    restored, _, _ = execute(blob, 21, exported)
    assert restored == factory
    rows = [{'case': 'export_then_import_factory_config', 'bytes': SIZE,
             'dma_chunks_each': 63, 'replies': ['0x1602', '0x1502']}]
    for mode in (0, 1):
        payload = bytearray(exported)
        payload[0] = mode
        local, _, _ = execute(blob, 21, payload)
        expected = bytearray(payload[:SIZE])
        for offset in (219, 221, 223, 225):
            expected[offset] = 100
        for offset in (220, 222, 224, 226, 227):
            expected[offset] = 0
        for offset, value in ((228, 0x3c0564), (232, 0x5a003c), (236, 0xc8005a)):
            struct.pack_into('<I', expected, offset, value)
        struct.pack_into('<H', expected, 240, 0x800)
        assert local == expected
        rows.append({'case': 'import_mode_defaults', 'mode_byte': mode,
                     'changed_offsets': [i for i, (a, b) in enumerate(zip(payload, local)) if a != b]})
    # Commands 23/24 share a separate 40-byte block. Preserve the same
    # firmware instance across set/get, and erase host contents before get.
    commands = iter((23, 24))
    local_blocks = []
    pattern = bytes(range(40))

    def submit(u):
        command = next(commands)
        u.mem_write(HOST, pattern if command == 23 else bytes(40))
        u.mem_write(MMIO+0x68, struct.pack('<II', 0x300 | command, HOST >> 8))

    def capture(u):
        local_blocks.append(bytes(u.mem_read(0x186254, 40)))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, sequence=(0, 0), boot_from_reset=True, via_trap=True,
                 dma_register_model=True, host_payload=b'\xa5'*4096,
                 host_submit=submit, host_handler=capture)
    assert result['returned'] and result['firmware_code_unmodified']
    assert result['function_stubs'] == []
    assert result['interrupts'] == [(0x1702, HOST >> 8), (0x1802, HOST >> 8)]
    assert local_blocks == [pattern, pattern]
    assert bytes.fromhex(result['host_payload_final']) == pattern+b'\xa5'*(4096-40)
    assert [d['direction'] for d in result['dma_commands']] == ['read']*10+['write']*10
    assert all(d['bytes'] == 4 for d in result['dma_commands'])
    rows.append({'case': 'opaque_40_byte_set_get_same_instance', 'bytes': 40,
                 'dma_chunks_each': 10, 'replies': ['0x1702', '0x1802']})
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'config_address': hex(CONFIG), 'config_bytes': SIZE,
                      'limitations': ['Only raw HAL-1 firmware code identity is established',
                                      'DMA/reset/IRQ device effects are synthetic; original firmware functions execute unchanged',
                                      'Mode-byte branches are observed, semantic field names and valid operating ranges are unknown',
                                      'No live configuration, physical boot, task execution or numerical inference is validated']}, indent=2))


if __name__ == '__main__':
    main()
