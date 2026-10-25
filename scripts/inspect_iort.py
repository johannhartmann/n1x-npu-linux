#!/usr/bin/env python3
"""Read bounded IORT nodes and DMA mappings; never evaluate AML or touch devices."""
import argparse
import json
import struct
from pathlib import Path


def decode(data):
    if len(data) < 48 or data[:4] != b'IORT':
        raise ValueError('not a complete IORT header')
    if struct.unpack_from('<I', data, 4)[0] != len(data) or sum(data) & 255:
        raise ValueError('invalid table length/checksum')
    count, position, reserved = struct.unpack_from('<III', data, 36)
    if reserved or position < 48 or count > (len(data)-48)//16:
        raise ValueError('invalid node inventory')
    nodes = []
    for _ in range(count):
        if position+16 > len(data):
            raise ValueError('truncated node header')
        kind, length, revision, identifier, maps, offset = struct.unpack_from('<BHBIII', data, position)
        if length < 16 or position+length > len(data):
            raise ValueError('invalid node bounds')
        if maps and (offset < 16 or offset+20*maps > length):
            raise ValueError('invalid mapping bounds')
        body_end = offset if maps else length
        row = dict(offset=position, type=kind, length=length, revision=revision, identifier=identifier)
        if kind == 1:
            if body_end < 30:
                raise ValueError('truncated named component')
            flags, properties, bits = struct.unpack_from('<IQB', data, position+16)
            name = data[position+29:position+body_end]
            if b'\0' not in name:
                raise ValueError('unterminated component name')
            row.update(name=name.split(b'\0', 1)[0].decode('ascii'), flags=flags,
                       memory_properties=properties, address_bits=bits)
        elif kind == 4:
            if body_end < 68:
                raise ValueError('truncated SMMUv3')
            row['smmu_base'] = struct.unpack_from('<Q', data, position+16)[0]
        row['mappings'] = [dict(zip(('input_base', 'id_count_minus_one', 'output_base',
                                    'output_reference', 'flags'),
                                   struct.unpack_from('<IIIII', data, position+offset+20*i)))
                           for i in range(maps)]
        nodes.append(row)
        position += length
    if position != len(data):
        raise ValueError('unconsumed table bytes')
    starts = {row['offset'] for row in nodes}
    if any(m['output_reference'] not in starts for row in nodes for m in row['mappings']):
        raise ValueError('mapping reference is not a node boundary')
    return dict(revision=data[8], node_count=count, nodes=nodes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('table', type=Path)
    args = parser.parse_args()
    print(json.dumps(decode(args.table.read_bytes()), indent=2))
