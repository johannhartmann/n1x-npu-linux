#!/usr/bin/env python3
"""Read-only status for the audited P16 NPU after Omarchy or kernel updates."""
import argparse
import hashlib
from pathlib import Path
import platform
import re
import subprocess

KERNEL = '7.2.8-23-rtx-spark-npu'
PACKAGE = 'n1x-npu-bootstrap-rtx-spark'
ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def check():
    issues = []
    facts = []
    dmi = Path('/sys/class/dmi/id')
    expected = {'sys_vendor': 'ASUS', 'product_name': 'ProArt P16 H7607BA',
                'bios_version': 'H7607BA.300'}
    for key, value in expected.items():
        path = dmi / key
        if not path.is_file() or path.read_text().strip() != value:
            issues.append(f'{key} differs from audited P16/BIOS; NPU qualification invalid')
    running = platform.release()
    facts.append(f'Kernel: {running}')
    if running != KERNEL:
        issues.append(f'running kernel is not the audited NPU kernel {KERNEL}')
    for name in ('linux-rtx-spark-npu', 'linux-rtx-spark-npu-headers'):
        if command('pacman', '-Q', name) is None:
            issues.append(f'{name} is not installed')
    module = Path('/usr/lib/modules') / KERNEL / 'updates/n1x-npu-bootstrap/n1x_npu_bootstrap.ko'
    owner = command('pacman', '-Qqo', str(module)) if module.is_file() else None
    if owner != PACKAGE:
        issues.append(f'NPU module is not owned by {PACKAGE}; rebuild/install the pinned package')
    elif (command('modinfo', '-F', 'vermagic', str(module)) or '').split(' ')[0] != KERNEL:
        issues.append('installed NPU module targets a different kernel')
    else:
        facts.append(f'Packaged NPU module: {module}')
    loaded = Path('/sys/module/n1x_npu_bootstrap/srcversion')
    if loaded.is_file() and module.is_file():
        installed_version = command('modinfo', '-F', 'srcversion', str(module))
        if loaded.read_text().strip() != installed_version:
            issues.append('loaded and packaged NPU modules differ; reboot before hardware use')
    elif loaded.is_file():
        issues.append('NPU module is loaded but has no persistent installed copy')
    nvidia_package = command('pacman', '-Q', 'nvidia-utils')
    nvidia_module = command('modinfo', '-k', KERNEL, '-F', 'version', 'nvidia')
    if not nvidia_package or not nvidia_module:
        issues.append('NVIDIA userspace or NPU-kernel module is missing')
    elif nvidia_package.split()[1].rsplit('-', 1)[0] != nvidia_module:
        issues.append('NVIDIA userspace and NPU-kernel module versions differ; rebuild external modules before reboot')
    for name, version in (('nvidia', '615.78.08'),
                          ('spbm', '0.3.0.rtxspark.5'), ('cpuid', '0.8.2')):
        selected = command('modinfo', '-k', KERNEL, '-F', 'filename', name)
        status = command('dkms', 'status', '-m', name, '-v', version, '-k', KERNEL)
        if not selected or '/updates/dkms/' not in selected or not status or 'installed' not in status:
            issues.append(f'{name} DKMS module is not installed and selected for {KERNEL}')
        else:
            facts.append(f'DKMS {name}: {selected}')
    facts.append('Physical inference requires a separate bounded post-reboot check')
    return facts, issues


def check_boot():
    issues = []
    menu = Path('/boot/limine.conf')
    if not menu.is_file():
        return ['Cannot read /boot/limine.conf; run --boot with root privileges']
    content = menu.read_text()
    defaults = re.findall(r'^default_entry:\s*(.*?)\s*$', content, re.M)
    if defaults not in (["Omarchy/linux-p16-asus-23-npu2"],
                        ["Omarchy/linux-rtx-spark-npu"]):
        issues.append('default is neither the known-working NPU2 entry nor the RTX Spark NPU candidate')
    if not re.search(r'^\s*//linux-rtx-spark-npu\s*$', content, re.M):
        issues.append('managed NPU boot entry missing')
    images = re.findall(r'boot\(\):(/EFI/Linux/[^#\s]+)#([a-f0-9]{128})', content)
    if not images:
        issues.append('no hashed Limine UKI paths found')
    for name, digest in images:
        image = Path('/boot') / name.lstrip('/')
        if not image.is_file() or hashlib.blake2b(image.read_bytes()).hexdigest() != digest:
            issues.append(f'Limine image missing or digest mismatch: {name}')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brief', action='store_true')
    parser.add_argument('--boot', action='store_true',
                        help='also verify Limine image digests; requires root')
    args = parser.parse_args()
    facts, issues = check()
    if args.boot:
        boot_issues = check_boot()
        issues.extend(boot_issues)
        if not boot_issues:
            facts.append('Limine image digests and known fallback entries verified')
    else:
        facts.append('Boot image/menu requires a separate privileged check (--boot)')
    if not args.brief:
        print('\n'.join(facts))
    if issues:
        print('NPU update check: ' + '; '.join(issues))
        return 1
    print('NPU update check: package, kernel and board gates pass; '
          + ('Limine verified; ' if args.boot else 'boot unverified; ')
          + 'physical inference unverified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
