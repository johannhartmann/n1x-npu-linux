#!/usr/bin/env python3
"""Replace the failed capture menu entry with a verified capture-OFF diagnostic.

Retains the old EFI file and backs up the menu. No reboot or default change.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from build_sroot_kernel import digest
from install_sroot_test import atomic_write
from verify_sroot_uki import verify

ROOT = Path(__file__).resolve().parents[1]
TITLE = 'N1X FRTS diagnostics - capture OFF'
EFI = 'omarchy_n1x-frts-diagnostic.efi'


def replace_entry(before, old_hash, new_hash, cmdline):
    if 'n1x_frts_snapshot' in cmdline or 'earlycon=efifb' not in cmdline.split():
        raise ValueError('Expected capture-OFF command line with early console')
    if any(c in cmdline for c in '\r\n\0'):
        raise ValueError('Invalid command line')
    if not all(re.fullmatch('[0-9a-f]{128}', h) for h in (old_hash, new_hash)):
        raise ValueError('Invalid EFI checksum')
    lines = before.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == '/N1X FRTS capture']
    if len(starts) != 1 or TITLE in before or EFI in before:
        raise ValueError('Expected one old entry and no diagnostic entry')
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('/')), len(lines))
    old = ''.join(lines[start:end])
    if 'omarchy_n1x-frts-capture.efi#' + old_hash not in old:
        raise ValueError('Old capture entry does not match installed record')
    entry = (f'/{TITLE}\ncomment: Diagnostic only; early RAM capture disabled\n'
             f'protocol: efi\npath: boot():/EFI/Linux/{EFI}#{new_hash}\ncmdline: {cmdline}\n\n')
    return ''.join(lines[:start]) + entry + ''.join(lines[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('diagnostic', type=Path)
    parser.add_argument('build', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Requires root')
    diagnostic, build = args.diagnostic.resolve(strict=True), args.build.resolve(strict=True)
    if not all(p.is_relative_to(ROOT / 'results') for p in (diagnostic, build)):
        parser.error('Expected local research build directories')
    record = json.loads((diagnostic / 'diagnostic.json').read_text())
    source = diagnostic / 'frts-diagnostic.efi'
    if record['capture_enabled'] is not False or digest(source) != record['sha256']:
        parser.error('Diagnostic artifact differs')
    verify(source, build / 'kernel/arch/arm64/boot/Image', diagnostic / 'cmdline')
    target = Path('/boot/EFI/Linux') / EFI
    checkpoint = Path('/var/lib/n1x-npu-test/frts-diagnostic')
    if shutil.disk_usage('/boot').free < source.stat().st_size + 64 * 1024 * 1024:
        parser.error('Insufficient EFI free space')
    with Path('/run/lock/boot-partition.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() or checkpoint.exists():
            parser.error('Diagnostic already installed; inspect it')
        old = json.loads(Path('/var/lib/n1x-npu-test/frts-capture/installed.json').read_text())
        if old['sha256'] != record['original_sha256'] or digest(Path(old['efi'])) != old['sha256']:
            parser.error('Original capture image differs')
        menu = Path('/boot/limine.conf')
        before = menu.read_text()
        new_hash = hashlib.blake2b(source.read_bytes()).hexdigest()
        after = replace_entry(before, old['blake2b'], new_hash, (diagnostic / 'cmdline').read_text().strip())
        checkpoint.mkdir(mode=0o700)
        (checkpoint / 'limine.conf.before').write_text(before)
        with source.open('rb') as inp, target.open('xb') as out:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if digest(target) != record['sha256']:
            raise RuntimeError('Deployed diagnostic image differs; menu not changed')
        atomic_write(menu, after.encode())
        if menu.read_text() != after:
            raise RuntimeError('Menu verification failed')
        (checkpoint / 'installed.json').write_text(json.dumps({
            'title': TITLE, 'efi': str(target), 'sha256': record['sha256'],
            'blake2b': new_hash, 'capture_enabled': False, 'physical_boot_verified': False,
        }, indent=2) + '\n')
    print('Installed ' + TITLE + '; failed capture entry retired; no reboot.')


if __name__ == '__main__':
    main()
