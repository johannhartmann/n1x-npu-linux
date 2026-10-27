#!/usr/bin/env python3
"""Add one manual capture entry using the proven npu2 kernel and module set."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil

from build_frts_snapshot import verify_installed_btf
from build_sroot_kernel import digest
from install_sroot_test import atomic_write
from prepare_npu2_capture import ROOT, RELEASE, capture_cmdline
from verify_sroot_uki import verify

TITLE = 'N1X NPU FRTS capture - npu2 with disk log'
EFI = 'omarchy_n1x-npu2-frts-capture.efi'


def append_entry(before, checksum, cmdline):
    if not re.fullmatch('[a-f0-9]{128}', checksum) or any(c in cmdline for c in '\r\n\0'):
        raise ValueError('Invalid checksum or command line')
    if TITLE in before or EFI in before:
        raise ValueError('Capture entry already exists')
    if re.findall(r'^default_entry:\s*(.*?)\s*$', before, re.M) != ['Omarchy/linux-p16-asus-23']:
        raise ValueError('Unexpected default')
    return before + ('\n' if not before.endswith('\n') else '') + (
        f'\n/{TITLE}\ncomment: Bounded early RAM read; no NPU upload; select manually\n'
        f'protocol: efi\npath: boot():/EFI/Linux/{EFI}#{checksum}\ncmdline: {cmdline}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('vm_result', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or platform.release() != RELEASE:
        parser.error('Requires root on the physically booted npu2 kernel')
    for name, value in (('sys_vendor', 'ASUS'), ('product_name', 'ProArt P16 H7607BA'),
                        ('bios_version', 'H7607BA.300')):
        if (Path('/sys/class/dmi/id') / name).read_text().strip() != value:
            parser.error('Unexpected machine or BIOS')
    capture, kernel = args.capture.resolve(), args.kernel.resolve()
    if not all(p.is_relative_to(ROOT / 'results') for p in (capture, kernel)):
        parser.error('Expected local research build directories')
    report = json.loads((capture / 'capture.json').read_text())
    vm = json.loads(args.vm_result.read_text())
    source = capture / 'frts-capture.efi'
    if report['release'] != RELEASE or digest(source) != report['sha256']:
        parser.error('Capture image differs from manifest')
    if (vm['uki_sha256'] != report['sha256'] or vm['kernel_sha256'] != report['kernel_sha256']
            or not vm.get('synthetic_frts_capture_verified') or not vm.get('early_capture_message_persisted')
            or not vm.get('initramfs_nvme_write_and_recovery_passed')):
        parser.error('Requires passing capture and persistent-log VM checks for this image')
    cmdline = (capture / 'cmdline').read_text().strip()
    if cmdline != capture_cmdline(Path('/proc/cmdline').read_text()):
        parser.error('Options must differ from this proven boot only by capture and log-buffer settings')
    installed = Path('/usr/lib/modules') / RELEASE
    if digest(installed / 'vmlinuz') != report['kernel_sha256']:
        parser.error('Installed kernel differs')
    verify_installed_btf(kernel, installed)
    verify(source, installed / 'vmlinuz', capture / 'cmdline', RELEASE)
    old = json.loads(Path('/var/lib/n1x-npu-test/npu2/state.json').read_text())
    base = Path(old['efi_path'])
    if old['uki_sha256'] != report['base_sha256'] or digest(base) != report['base_sha256']:
        parser.error('Installed capture-OFF fallback differs')
    target = Path('/boot/EFI/Linux') / EFI
    checkpoint = Path('/var/lib/n1x-npu-test/npu2-frts-capture')
    checksum = hashlib.blake2b(source.read_bytes()).hexdigest()
    with Path('/run/lock/boot-partition.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() or checkpoint.exists():
            parser.error('Capture installation already exists; inspect it')
        if shutil.disk_usage('/boot').free < source.stat().st_size + 64 * 1024 * 1024:
            parser.error('Insufficient EFI free space')
        menu = Path('/boot/limine.conf')
        before = menu.read_text()
        for name in (base.name, 'omarchy_linux-p16-asus-23.efi'):
            image = Path('/boot/EFI/Linux') / name
            if name + '#' + hashlib.blake2b(image.read_bytes()).hexdigest() not in before:
                parser.error('Fallback entry/checksum missing or changed')
        after = append_entry(before, checksum, cmdline)
        checkpoint.mkdir(mode=0o700)
        atomic_write(checkpoint / 'limine.conf.before', before.encode())
        atomic_write(target, source.read_bytes())
        if digest(target) != report['sha256'] or menu.read_text() != before:
            raise RuntimeError('EFI verification failed or menu changed; entry not installed')
        atomic_write(menu, after.encode())
        if menu.read_text() != after:
            raise RuntimeError('Installed menu verification failed')
        atomic_write(checkpoint / 'installed.json', (json.dumps({
            'title': TITLE, 'efi': str(target), 'sha256': report['sha256'], 'blake2b': checksum,
            'kernel_sha256': report['kernel_sha256'], 'capture_enabled': True,
            'default_unchanged': True, 'kernel_and_modules_unchanged': True,
            'physical_capture_verified': False,
        }, indent=2) + '\n').encode())
        os.sync()
    print('Installed manual npu2 capture entry; both working fallbacks preserved; no reboot.')


if __name__ == '__main__':
    main()
