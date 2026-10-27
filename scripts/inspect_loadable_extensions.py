#!/usr/bin/env python3
"""Trace modern metadata fields through the original Windows deserializer.

Mutations exist only in the emulator's input bytes. No device, firmware upload
or compiler execution. Field names remain conservative where semantics are open.
"""
import hashlib
import json
import struct
import pefile
from unicorn import UC_HOOK_MEM_READ
from unicorn import arm64_const as registers
import inspect_full_deserialization as full
from inspect_loadable import Buffer
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH
from inspect_content_materialization import MODEL
from verify_relu_loadable import LOADABLE_HASH


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(path))
    data = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == LOADABLE_HASH
    view, root = Buffer(data), struct.unpack_from('<I', data)[0]
    root_version = view.field(root, 9)
    blobs = {view.string(t, 0): view.field(t, 6) for t in view.tables(root, 5)}
    assert data[root_version:root_version+3] == bytes([3, 0, 0])
    assert all(data[p:p+3] == bytes([0, 0, 0] if name.startswith('property_') else [3, 16, 0])
               for name, p in blobs.items())
    original_uc = full.Uc
    rows = []
    for name in ('original', 'root_version_4_5_6', 'root_version_missing', 'blob_extension_changed', 'blob_extension_missing'):
        candidate = bytearray(data)
        expected = (3, 0, 0)
        if name == 'root_version_4_5_6':
            expected = (4, 5, 6)
            candidate[root_version:root_version+3] = bytes(expected)
        elif name == 'root_version_missing':
            expected = (0, 0, 0)
            vtable = root - struct.unpack_from('<i', data, root)[0]
            struct.pack_into('<H', candidate, vtable+4+2*9, 0)
        elif name == 'blob_extension_changed':
            for p in blobs.values():
                candidate[p:p+3] = b'\x55\xaa\x33'
        elif name == 'blob_extension_missing':
            for table in view.tables(root, 5):
                vtable = table - struct.unpack_from('<i', data, table)[0]
                struct.pack_into('<H', candidate, vtable+4+2*6, 0)
        reads = set()
        fields = [('root_field_9', root_version), *[(k, v) for k, v in blobs.items()]]
        def create_uc(*args, **kwargs):
            u = original_uc(*args, **kwargs)
            def observe(uc, access, address, size, value, user):
                for field, offset in fields:
                    if address < MODEL+offset+3 and MODEL+offset < address+size:
                        reads.add((field, uc.reg_read(registers.UC_ARM64_REG_PC)-BASE,
                                   address-MODEL, size))
            u.hook_add(UC_HOOK_MEM_READ, observe)
            return u
        full.Uc = create_uc
        try:
            result = full.execute(pe, bytes(candidate), 0xa5, expected_context_version=expected)
        finally:
            full.Uc = original_uc
        assert result['deserializer_status'] == 0
        root_reads = sorted(row for row in reads if row[0] == 'root_field_9')
        blob_reads = sorted(row for row in reads if row[0] != 'root_field_9')
        if name == 'root_version_missing':
            assert not root_reads
        else:
            assert {pc for _, pc, _, _ in root_reads} == {0x88aac, 0x88ab0, 0x88ab4}
        if name == 'blob_extension_missing':
            assert not blob_reads
            assert all(v == [3, 13, 0] for v in result['content_auxiliary_versions'].values())
        else:
            assert len(blob_reads) == 24
            assert {pc for _, pc, _, _ in blob_reads} == {0x88468, 0x8846c, 0x88470}
            if name == 'blob_extension_changed':
                assert all(v == [0x55, 0xaa, 0x33] for v in result['content_auxiliary_versions'].values())
        rows.append(dict(case=name, status=result['deserializer_status'],
                         context_version=result['context_version'],
                         statistics_status=result['stats_status'],
                         statistics_headers=result['stats_headers'],
                         root_field_reads=[dict(field=field, pc=hex(pc), offset=offset, bytes=size)
                                           for field, pc, offset, size in root_reads],
                         blob_extension_reads=len(blob_reads),
                         content_auxiliary_versions=result['content_auxiliary_versions'],
                         all_materializers_completed=True,
                         firmware_code_or_hardware_execution=False))
    print(json.dumps(dict(passed=True, hardware_access=False, runtime_sha256=RUNTIME_HASH,
                          loadable_sha256=LOADABLE_HASH, cases=rows,
                          conclusion=['Root field 9 is a three-byte version copied into context +0x1a8',
                                      'Changing root version to 4.5.6 still deserializes, but statistics parsing returns 2 before reading its header',
                                      'Missing root field produces 0.0.0 and prevents statistics header parsing for this fixture',
                                      'Blob field 6 is a three-byte version copied to content node +0x6b, default 3.13.0',
                                      'Altered auxiliary versions survive full deserialization; exact downstream meaning remains unproven',
                                      'This does not establish later runtime/firmware acceptance of altered versions']), indent=2))


if __name__ == '__main__':
    main()
