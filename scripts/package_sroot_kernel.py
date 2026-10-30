#!/usr/bin/env python3
"""Package a verified NPU kernel with the reviewed P16 packaging recipe.

Creates co-installable kernel/headers packages only. Never installs packages or
changes boot entries. The original P16 recipe and source tree remain untouched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from build_sroot_kernel import digest

RECIPE_HASH = '7f878f7b87b96dfccb8e31ed36a69a341c1a370e7f336dfb9e48421e3868fbda'
def package_base(release):
    if release not in ('7.2.8-23-omarchy-p16-asus-npu1',
                       '7.2.8-23-omarchy-p16-asus-npu2',
                       '7.2.8-23-rtx-spark-npu'):
        raise ValueError('unsupported qualified NPU kernel release')
    if release == '7.2.8-23-rtx-spark-npu':
        return 'linux-rtx-spark-npu'
    return 'linux-p16-asus-23-' + release.rsplit('-', 1)[1]


def audit_package(package, release, image_pin, config_pin):
    pkgbase = package_base(release)
    names = subprocess.check_output(['bsdtar', '-tf', str(package)], text=True).splitlines()
    info = subprocess.check_output(['bsdtar', '-xOf', str(package), '.PKGINFO'], text=True)
    fields = {}
    for line in info.splitlines():
        if ' = ' in line:
            key, value = line.split(' = ', 1)
            fields.setdefault(key, []).append(value)
    name = fields.get('pkgname', [''])[0]
    if name not in (pkgbase, pkgbase+'-headers') or fields.get('arch') != ['aarch64']:
        raise ValueError('unexpected package identity/architecture')
    if 'conflict' in fields or 'replaces' in fields:
        raise ValueError('test package must not replace/conflict with existing kernels')
    allowed = ('usr/lib/modules/'+release+'/', 'boot/dtbs/'+pkgbase+'/', 'usr/src/'+pkgbase)
    parents = {'usr/', 'usr/lib/', 'usr/lib/modules/', 'usr/src/', 'boot/', 'boot/dtbs/',
               '.PKGINFO', '.BUILDINFO', '.MTREE'}
    for entry in names:
        if '..' in Path(entry).parts or Path(entry).is_absolute() or not (
                entry in parents or entry.startswith(allowed)):
            raise ValueError('package path escapes the isolated release: '+entry)
    prefix = 'usr/lib/modules/'+release+'/'
    def read(entry):
        return subprocess.check_output(['bsdtar', '-xOf', str(package), prefix+entry])
    if name == pkgbase:
        if hashlib.sha256(read('vmlinuz')).hexdigest() != image_pin:
            raise ValueError('packaged kernel Image differs from the verified build')
        if not any('spi-hid' in entry and '.ko' in entry for entry in names):
            raise ValueError('SPI HID modules missing from the test package')
    else:
        if hashlib.sha256(read('build/.config')).hexdigest() != config_pin:
            raise ValueError('packaged headers use a different configuration')
        if read('build/version').decode().strip() != release:
            raise ValueError('packaged headers use a different release')
    return {'name': name, 'entries': len(names), 'unique_paths': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('recipe', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    kernel, recipe, destination = args.kernel.resolve(), args.recipe.resolve(), args.destination.absolute()
    if destination.exists() or destination.is_symlink():
        parser.error('destination must be new; inspect an existing packaging process first')
    if digest(recipe) != RECIPE_HASH:
        parser.error('P16 packaging recipe differs from the reviewed version')
    manifest = json.loads((kernel/'npu-build-manifest.json').read_text())
    release = manifest['expected_release']
    pkgbase = package_base(release)
    if manifest['stage'] != 'built':
        parser.error('requires the completed qualified NPU kernel build')
    if digest(kernel/'arch/arm64/boot/Image') != manifest['image_sha256']:
        parser.error('kernel Image differs from the verified build')
    if digest(kernel/'.config') != manifest['source_pins']['.config']:
        parser.error('kernel configuration differs from the verified build')
    if os.geteuid() == 0:
        parser.error('makepkg must run as the normal user')
    text = recipe.read_text()
    for key in ('pkgbase', '_kernel'):
        old = key+'=linux-p16-asus-23\n'
        if text.count(old) != 1:
            parser.error('unexpected package-name assignment')
        text = text.replace(old, key+'='+pkgbase+'\n')
    # Repackage already built artifacts; there are no source downloads/build
    # dependencies in this derived recipe. The actual packaging functions are
    # retained, including versioned file placement and ARM64 Image selection.
    text += '''\n# Verified prebuilt artifact packaging only.\nsource=()\nsource_aarch64=()\nsha256sums=()\nsha256sums_aarch64=()\nb2sums=()\nb2sums_aarch64=()\nmakedepends=(binutils kmod zstd)\nCOMPRESSZST=(zstd -c -T4 -6 -)\n'''
    destination.mkdir()
    (destination/'PKGBUILD').write_text(text)
    (destination/'src').mkdir()
    (destination/'src/linux-7.2.8').symlink_to(kernel, target_is_directory=True)
    # The base recipe reads this small file for the installation directory.
    (kernel/'version').write_text(release+'\n')
    env = dict(os.environ, PKGEXT='.pkg.tar.zst', MAKEFLAGS='-j8')
    subprocess.run(['makepkg', '--repackage', '--noextract', '--noprepare', '--noconfirm'],
                   cwd=destination, env=env, check=True)
    packages = sorted(destination.glob('*.pkg.tar.zst'))
    if len(packages) != 2:
        raise RuntimeError('expected exactly kernel and headers packages')
    report = {'release': release, 'installed': False, 'recipe_sha256': RECIPE_HASH,
              'packages': [{'path': str(p), 'sha256': digest(p),
                            'audit': audit_package(p, release, manifest['image_sha256'],
                                                   manifest['source_pins']['.config'])}
                           for p in packages]}
    (destination/'package-manifest.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Built two co-installable test-kernel packages; nothing installed', flush=True)


if __name__ == '__main__':
    main()
