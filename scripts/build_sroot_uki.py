#!/usr/bin/env python3
"""Build the NPU test UKI in a private module root; never touch /boot or /etc.

Runs only mkinitcpio with real root privileges for initramfs metadata. --nopost prevents installed
mkinitcpio post hooks from deploying a boot image or changing the boot menu.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_sroot_kernel import digest
from package_sroot_kernel import audit_package, PKGBASE
from verify_sroot_uki import verify

PINS = {'DSDT.aml': '80e0ad4a051704003d09029cb600a9814e1cd370ce774b9749dc09d2232b5aaf',
        'SSDT.aml': '9b28ffc14c19f8b7ea2278de88ff9ec070f5f70401107d81fef067a916aa9695'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('packages', type=Path)
    parser.add_argument('external_modules', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    kernel = args.kernel.resolve()
    output = args.destination.absolute()
    if output.exists() or output.is_symlink():
        parser.error('destination must be new; inspect existing build logs before resuming')
    manifest = json.loads((kernel/'npu-build-manifest.json').read_text())
    release = manifest['expected_release']
    if release != '7.2.8-23-omarchy-p16-asus-npu1' or manifest['stage'] != 'built':
        parser.error('requires the completed isolated NPU kernel build')
    external = json.loads((args.external_modules/'external-module-build.json').read_text())
    if external['release'] != release or external['stage'] != 'built':
        parser.error('external modules do not match the test kernel')
    packages = json.loads((args.packages/'package-manifest.json').read_text())
    if packages['release'] != release:
        parser.error('package release mismatch')
    package = next(Path(p['path']) for p in packages['packages']
                   if audit_package(Path(p['path']), release, manifest['image_sha256'],
                                    manifest['source_pins']['.config'])['name'] == PKGBASE)
    for row in external['modules']:
        if digest(Path(row['path'])) != row['sha256'] or row['vermagic'].split()[0] != release:
            parser.error('external module hash/release mismatch')
    for name, pin in PINS.items():
        if digest(Path('/etc/initcpio/p16-acpi')/name) != pin:
            parser.error('installed ACPI overrides differ from audited tables')
    output.mkdir()
    root = output/'root'
    root.mkdir()
    subprocess.run(['bsdtar', '-xf', str(package.resolve()), '-C', str(root)], check=True)
    (root/'lib').symlink_to('usr/lib')
    modules = root/'usr/lib/modules'/release
    updates = modules/'updates/dkms'
    updates.mkdir(parents=True)
    for row in external['modules']:
        shutil.copy2(row['path'], updates/Path(row['path']).name)
    subprocess.run(['depmod', '-b', str(root), release], check=True)
    for name in ('nvidia', 'nvidia_drm', 'hid_asus', 'spi_hid_acpi', 'i2c_mt65xx', 'i2c_hid_acpi'):
        actual = subprocess.check_output(['modinfo', '-b', str(root), '-k', release,
                                         '-F', 'vermagic', name], text=True).split()[0]
        if actual != release:
            raise RuntimeError('staged module release mismatch: '+name)
    hooks = output/'initcpio'
    (hooks/'install').mkdir(parents=True)
    tables = output/'acpi'
    tables.mkdir()
    additions = []
    for name in PINS:
        shutil.copy2(Path('/etc/initcpio/p16-acpi')/name, tables/name)
        additions.append('    add_file_early '+shlex.quote(str(tables/name))+' /kernel/firmware/acpi/'+name)
    (hooks/'install/npu-test-acpi').write_text('''#!/usr/bin/env bash
build() {
    [[ $KERNELVERSION == 7.2.8-23-omarchy-p16-asus-npu1 ]] || return 1
'''+ '\n'.join(additions)+'''
}
help() { echo 'Pinned P16 ACPI overrides for the isolated NPU test kernel'; }
''')
    configs = [Path('/etc/mkinitcpio.conf')]
    directory = Path('/etc/mkinitcpio.conf.d')
    names = subprocess.check_output(['sort', '-Vu'], input='\n'.join(p.name for p in directory.glob('*.conf'))+'\n', text=True).splitlines()
    configs += [directory/name for name in names]
    conf = output/'mkinitcpio.conf'
    snapshots = [(p, p.read_bytes()) for p in configs]
    conf.write_bytes(b'\n'.join(data for _, data in snapshots)+
                     b'\nMODULES+=(spi_hid_acpi)\nHOOKS+=(npu-test-acpi)\n')
    cmdline = output/'cmdline'
    cmdline.write_text(Path('/proc/cmdline').read_text())
    image = output/'linux-p16-asus-23-npu1.efi'
    command = ['pkexec', '/usr/bin/python3', str(Path(__file__).with_name('run_sroot_mkinitcpio.py')), str(output)]
    log = output/'mkinitcpio.log'
    with log.open('w') as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
    text = log.read_text()
    if any(token in text for token in ('==> ERROR:', 'errors were encountered during the build', 'libfakeroot internal error')):
        raise RuntimeError('mkinitcpio reported build errors despite its exit status; inspect '+str(log))
    verified = verify(image, modules/'vmlinuz', cmdline)
    report = {'release': release, 'installed': False, 'uki': str(image),
              'sha256': digest(image), 'kernel_image_sha256': manifest['image_sha256'],
              'configs': {str(p): hashlib.sha256(data).hexdigest() for p, data in snapshots},
              'boot_menu_sha256': digest(output/'boot-menu-before.txt'), 'acpi_sha256': PINS,
              'verification': verified}
    (output/'uki-build-manifest.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Built test UKI outside /boot; post hooks disabled; no deployment', flush=True)


if __name__ == '__main__':
    main()
