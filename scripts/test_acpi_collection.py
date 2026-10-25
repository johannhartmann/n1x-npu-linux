#!/usr/bin/env python3
"""Check read-only collection against isolated static/dynamic table fixtures."""
import json
import struct
import tempfile
import subprocess
from pathlib import Path
from collect_acpi import snapshot


def table(signature, body=b'', revision=1):
    data = bytearray(struct.pack('<4sIBB6s8sI4sI', signature, 36+len(body), revision, 0,
                                 b'TEST  ', b'FIXTURE ', 1, b'TEST', 1)+body)
    data[9] = -sum(data) & 255
    return bytes(data)


def iort_fixture():
    name = b'\\_SB_.BUS0.TEST\0'
    body = struct.pack('<IQB', 0, 1, 40)+name
    body += bytes((-len(body)) % 4)
    named_size = 16+len(body)+20
    smmu_offset = 48+named_size
    named = struct.pack('<BHBIII', 1, named_size, 4, 1, 1, 16+len(body))+body
    named += struct.pack('<IIIII', 0, 0, 0x42, smmu_offset, 1)
    smmu = struct.pack('<BHBIII', 4, 68, 4, 2, 0, 0)
    smmu += struct.pack('<QIIQIIIIIII', 0x12340000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    return table(b'IORT', struct.pack('<III', 2, 48, 0)+named+smmu, revision=6)


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-acpi-copy-') as tmp:
        root = Path(tmp)
        source = root/'tables'
        (source/'dynamic').mkdir(parents=True)
        expected = {'DSDT.aml': table(b'DSDT'), 'IORT.aml': iort_fixture(),
                    'SSDT1.aml': table(b'SSDT'), 'dynamic-SSDT1.aml': table(b'SSDT', b'\0')}
        for name, data in expected.items():
            path = source/'dynamic'/name[8:-4] if name.startswith('dynamic-') else source/name[:-4]
            path.write_bytes(data)
        (source/'APIC').write_bytes(b'not selected')
        output = root/'snapshot'
        rows = snapshot(source, output)
        assert len(rows) == 4
        assert {path.name: path.read_bytes() for path in output.glob('*.aml')} == expected
        manifest = json.loads((output/'manifest.json').read_text())
        assert manifest['tables'] == rows
        disassembly = subprocess.run(['iasl', '-d', str(output/'IORT.aml')],
                       capture_output=True, text=True, check=True)
        decoded = (output/'IORT.dsl').read_text()
        report = (disassembly.stdout+disassembly.stderr+decoded).lower()
        assert not any(word in report for word in ('unsupported', 'invalid', 'warning', 'error'))
        assert 'TEST' in decoded and '12340000' in decoded and '00000042' in decoded
        try:
            snapshot(source, output)
        except FileExistsError:
            pass
        else:
            raise AssertionError('Existing snapshot was reused')
        for bad in (b'bad', table(b'SSDT'), table(b'IORT', bytes(12))[:-1],
                    table(b'IORT', bytes(12))[:-1]+b'\xff'):
            (source/'IORT').write_bytes(bad)
            rejected = root/'rejected'
            try:
                snapshot(source, rejected)
            except ValueError:
                pass
            else:
                raise AssertionError('Malformed table accepted')
            assert not rejected.exists()
        (source/'IORT').unlink()
        try:
            snapshot(source, root/'missing')
        except ValueError:
            pass
        else:
            raise AssertionError('Missing IORT accepted')
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'cases': ['static and dynamic tables', 'same-name collision isolation',
                                'existing snapshot protected', 'malformed headers/length/checksum',
                                'required IORT missing', 'ACPICA named-component/SMMUv3 fixture decode']}, indent=2))


if __name__ == '__main__':
    main()
