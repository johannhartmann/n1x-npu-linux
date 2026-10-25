#!/usr/bin/env python3
"""Validate IORT padding, routes and malformed bounds using synthetic tables."""
import struct
from inspect_iort import decode
from test_acpi_collection import iort_fixture


def patched(data, offset, fmt, value):
    data = bytearray(data)
    struct.pack_into(fmt, data, offset, value)
    data[9] = 0
    data[9] = -sum(data) & 255
    return bytes(data)


def main():
    data = iort_fixture()
    report = decode(data)
    named, smmu = report['nodes']
    assert report['node_count'] == 2
    assert named['name'] == r'\_SB_.BUS0.TEST'
    assert named['address_bits'] == 40 and named['memory_properties'] == 1
    assert named['mappings'] == [dict(input_base=0, id_count_minus_one=0,
                                    output_base=0x42, output_reference=smmu['offset'], flags=1)]
    assert smmu['smmu_base'] == 0x12340000
    mapping = 48+struct.unpack_from('<I', data, 60)[0]
    bad_tables = [data[:35], data[:-1], data[:-1]+bytes([data[-1]^1]),
                  patched(data, 4, '<I', len(data)-1),
                  patched(data, 36, '<I', 3), patched(data, 40, '<I', 47),
                  patched(data, 44, '<I', 1), patched(data, 49, '<H', 0),
                  patched(data, 60, '<I', len(data)),
                  patched(data, mapping+12, '<I', smmu['offset']+1),
                  patched(data, smmu['offset']+1, '<H', 67)]
    # Remove every name terminator/padding byte, retaining valid table checksum.
    bad = bytearray(data)
    bad[77:mapping] = b'X'*(mapping-77)
    bad[9] = 0
    bad[9] = -sum(bad) & 255
    bad_tables.append(bytes(bad))
    for index, bad in enumerate(bad_tables):
        try:
            decode(bad)
        except (ValueError, UnicodeDecodeError):
            continue
        raise AssertionError(f'malformed fixture {index} accepted')
    print(f'IORT fixture and {len(bad_tables)} malformed-table checks passed; no hardware access')


if __name__ == '__main__':
    main()
