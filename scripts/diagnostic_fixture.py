#!/usr/bin/env python3
"""Construct RAM-only diagnostic allocations from the provisional loadable projection."""
import hashlib
from inspect_loadable import Buffer, inspect

DIAGNOSTIC_HASH = '51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543'


def build(data, host_base):
    assert hashlib.sha256(data).hexdigest() == DIAGNOSTIC_HASH
    metadata, view = inspect(data), Buffer(data)
    blobs = {}
    for table in view.tables(view.u('<I', 0), 5):
        extent = view.vector(table, 5, 1)
        blobs[view.string(table, 0)] = data[extent.start:extent.stop]
    cursor, layout = 1024, {}
    for memory in metadata['memory']:
        alignment = memory['alignment']
        assert alignment and alignment & (alignment-1) == 0
        address = (host_base+cursor+alignment-1) & -alignment
        # Explicit fixture padding for cache reads; logical size remains unchanged.
        backing = (max(memory['size'], 256)+255) & ~255
        assert memory['id'] not in layout
        layout[memory['id']] = {'address': address, 'offset': address-host_base,
                                'bytes': memory['size'], 'backing': backing, 'alignment': alignment,
                                'contents': memory['contents']}
        cursor = address-host_base+backing
    payload = bytearray(cursor)
    for memory in metadata['memory']:
        allocation = layout[memory['id']]
        assert len(memory['contents']) == len(memory['content_offsets'])
        for name, offset in zip(memory['contents'], memory['content_offsets']):
            blob = blobs[name]
            assert offset+len(blob) <= allocation['bytes']
            start = allocation['offset']+offset
            payload[start:start+len(blob)] = blob
    addresses = []
    for entry in metadata['addresses']:
        assert entry['id'] == len(addresses)
        allocation = layout[entry['memory_id']]
        assert entry['offset']+entry['size'] <= allocation['bytes']
        addresses.append(allocation['address']+entry['offset'])
    assert metadata['tasks'][0]['address_ids'] == list(range(9))
    return payload, addresses, layout
