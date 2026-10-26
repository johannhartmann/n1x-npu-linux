#!/usr/bin/env python3
"""Read-only recheck of the installed NPU test boot entry before manual boot.

Requires access to /boot and the installation checkpoint. Writes only objcopy
scratch files in a temporary directory; does not install, load or reboot.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess

from install_sroot_test import RELEASE, TITLE, EFI_NAME, render_entry
from verify_sroot_uki import verify
from package_sroot_kernel import package_base

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', default=RELEASE)
    parser.add_argument('--uki-directory', type=Path, default=ROOT/'results/npu1-uki-v3')
    args = parser.parse_args()
    release = args.release
    pkgbase = package_base(release)
    checkpoint = Path('/var/lib/n1x-npu-test')/release.rsplit('-', 1)[1]
    state = json.loads((checkpoint/'state.json').read_text())
    uki_dir = args.uki_directory.resolve()
    manifest = json.loads((uki_dir/'uki-build-manifest.json').read_text())
    modules = Path('/usr/lib/modules')/release
    image = Path('/boot/EFI/Linux')/('omarchy_' + pkgbase + '.efi')
    require(state['release'] == manifest['release'] == release, 'release mismatch')
    require(state['stage'] == 'ready-for-manual-boot', 'installation did not finish')
    require(state['efi_path'] == str(image), 'unexpected installed EFI path')
    require(sha(image) == state['uki_sha256'] == manifest['sha256'], 'installed UKI changed')
    require(sha(modules/'vmlinuz') == manifest['kernel_image_sha256'], 'installed kernel changed')
    require(subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '/boot'],
                                   text=True).strip() == 'vfat', 'unexpected boot mount')
    for key, expected in (('sys_vendor', 'ASUS'), ('product_name', 'ProArt P16 H7607BA'),
                          ('bios_version', 'H7607BA.300')):
        require((Path('/sys/class/dmi/id')/key).read_text().strip() == expected,
                'machine or BIOS differs from the audited device')
    before = (checkpoint/'limine.conf.before').read_text()
    require(hashlib.sha256(before.encode()).hexdigest() == manifest['boot_menu_sha256'],
            'original menu snapshot mismatch')
    blake = hashlib.blake2b(image.read_bytes()).hexdigest()
    menu = Path('/boot/limine.conf').read_text()
    require(menu == render_entry(before, blake, (uki_dir/'cmdline').read_text().strip(), release),
            'live menu differs from the verified append-only installation')
    fallback = re.findall(r'boot\(\):(/EFI/Linux/omarchy_linux-p16-asus-23\.efi)#([a-f0-9]{128})', before)
    require(len(fallback) == 1, 'missing or ambiguous working-kernel fallback')
    old_path, old_hash = fallback[0]
    require(hashlib.blake2b((Path('/boot')/old_path.lstrip('/')).read_bytes()).hexdigest() == old_hash,
            'working fallback EFI digest mismatch')
    expected_modules = {'nvidia.ko', 'nvidia-drm.ko', 'nvidia-modeset.ko', 'nvidia-uvm.ko',
                        'nvidia-peermem.ko', 'spbm.ko', 'cpuid.ko', 'n1x_npu_observe.ko'}
    require(len(state['modules']) == len(expected_modules) and
            {Path(row['path']).name for row in state['modules']} == expected_modules,
            'incomplete installed-module checkpoint')
    for row in state['modules']:
        path = Path(row['path'])
        require(path.parent == modules/'updates/npu-test', 'unexpected installed module path')
        require(sha(path) == row['sha256'], 'installed module changed: '+path.name)
        require(subprocess.check_output(['modinfo', '-F', 'vermagic', str(path)],
                                       text=True).split()[0] == release, 'module release mismatch')
    for name in ('nvidia', 'nvidia_drm', 'spbm', 'cpuid', 'spi_hid_acpi', 'hid_asus', 'n1x_npu_observe'):
        require(subprocess.check_output(['modinfo', '-k', release, '-F', 'vermagic', name],
                                       text=True).split()[0] == release,
                'resolved module release mismatch: '+name)
        lines = subprocess.check_output(['modprobe', '-S', release, '--show-depends', name], text=True)
        for line in lines.splitlines():
            if line.startswith('insmod '):
                require(Path(line.split()[1]).is_file(), 'missing dependency: '+line)
    verified = verify(image, modules/'vmlinuz', uki_dir/'cmdline', release)
    require(Path('/boot/limine.conf').read_text() == menu and sha(image) == manifest['sha256'],
            'boot files changed during audit')
    title = TITLE if release == RELEASE else 'N1X NPU diagnostics - disk log (capture OFF)'
    print(json.dumps(dict(ready_for_manual_boot=True, entry=title, target_kernel=release,
        running_kernel=platform.release(), uki_sha256=sha(image),
        fallback_digest_valid=True, menu_defaults_preserved=True,
        installed_modules_checked=len(state['modules']), uki_verification=verified,
        physical_boot_verified=False, hardware_access=False, system_modified=False), indent=2))


if __name__ == '__main__':
    main()
