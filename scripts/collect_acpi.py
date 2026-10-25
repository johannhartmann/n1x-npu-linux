#!/usr/bin/env python3
"""Copy ACPI table bytes and hashes without evaluating AML or binding devices."""
import argparse
import hashlib
import json
import os
import struct
from pathlib import Path


def inventory(source):
    entries = {}
    for directory, prefix in ((source, ''), (source/'dynamic', 'dynamic-')):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and (path.name in ('DSDT', 'IORT') or path.name.startswith('SSDT')):
                entries[prefix+path.name+'.aml'] = path
    return entries


def snapshot(source, output):
    before = inventory(source)
    if 'DSDT.aml' not in before or 'IORT.aml' not in before:
        raise ValueError('DSDT and IORT are required for this machine audit')
    tables, rows = {}, []
    for name, path in before.items():
        data = path.read_bytes()
        expected = b'IORT' if path.name == 'IORT' else b'DSDT' if path.name == 'DSDT' else b'SSDT'
        if len(data) < 36 or data[:4] != expected:
            raise ValueError(f'{path}: invalid ACPI header/signature')
        if struct.unpack_from('<I', data, 4)[0] != len(data) or sum(data) & 255:
            raise ValueError(f'{path}: invalid ACPI length/checksum')
        tables[name] = data
        rows.append({'file': name, 'source': str(path), 'bytes': len(data),
                     'sha256': hashlib.sha256(data).hexdigest()})
    # Reject a dynamic-table inventory change rather than claiming a complete set.
    if inventory(source) != before:
        raise RuntimeError('ACPI table inventory changed during collection; no snapshot written')
    # Read privileged sysfs first, then write as the invoking sudo user. This
    # keeps analysis files editable without running the disassembler as root.
    if os.geteuid() == 0 and 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
        uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in tables.items():
        with (output/name).open('xb') as stream:
            stream.write(data)
    with (output/'manifest.json').open('x') as stream:
        json.dump({'scope': 'Read-only table copy; no AML evaluation or hardware access',
                   'tables': rows}, stream, indent=2)
        stream.write('\n')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New snapshot directory; existing directories are refused')
    parser.add_argument('--source', type=Path, default=Path('/sys/firmware/acpi/tables'))
    args = parser.parse_args()
    try:
        rows = snapshot(args.source, args.output)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'{error}\n')
    print(json.dumps({'copied_tables': len(rows), 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
