#!/usr/bin/env python3
"""Install only the prepared NPU test variant; preserve the existing boot default.

No reboot, firmware upload or NPU register access. Package hooks are masked for
this transaction only; the pre-verified UKI is deployed as one additional entry.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
from build_sroot_kernel import digest
from package_sroot_kernel import audit_package
from verify_sroot_uki import verify

ROOT = Path(__file__).resolve().parents[1]
RELEASE = '7.2.8-23-omarchy-p16-asus-npu1'
TITLE = 'N1X NPU test'
EFI_NAME = 'omarchy_linux-p16-asus-23-npu1.efi'


def render_entry(before, blake, cmdline):
    if not re.fullmatch('[0-9a-f]{128}', blake):
        raise ValueError('invalid EFI BLAKE2b digest')
    if any(c in cmdline for c in '\n\r\0'):
        raise ValueError('invalid kernel command line')
    if re.search(r'^/'+re.escape(TITLE)+r'\s*$', before, re.M) or EFI_NAME in before:
        raise ValueError('test entry already exists')
    if re.findall(r'^default_entry:\s*(.*?)\s*$', before, re.M) != ['Omarchy/linux-p16-asus-23']:
        raise ValueError('working kernel is not the recorded boot default')
    addition = ('\n' if not before.endswith('\n') else '')+'\n/'+TITLE+'\n'
    addition += 'comment: Experimental SROOT DMA kernel; select manually\nprotocol: efi\n'
    addition += 'path: boot():/EFI/Linux/'+EFI_NAME+'#'+blake+'\ncmdline: '+cmdline+'\n'
    after = before+addition
    keys = r'^(?:default_entry|timeout|quiet):.*$'
    if re.findall(keys, before, re.M) != re.findall(keys, after, re.M):
        raise ValueError('boot defaults changed')
    return after


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('uki_directory', type=Path)
    parser.add_argument('kernel_directory', type=Path)
    parser.add_argument('package_directory', type=Path)
    parser.add_argument('external_directory', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('requires root')
    if platform.release() != '7.2.8-23-omarchy-p16-asus':
        parser.error('requires the recorded working kernel')
    for key, value in [('sys_vendor', 'ASUS'), ('product_name', 'ProArt P16 H7607BA'),
                       ('bios_version', 'H7607BA.300')]:
        if (Path('/sys/class/dmi/id')/key).read_text().strip() != value:
            parser.error('machine or BIOS differs from the audited P16')
    if subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '/boot'], text=True).strip() != 'vfat':
        parser.error('/boot is not the expected FAT EFI mount')
    kernel = args.kernel_directory.resolve()
    uki_dir = args.uki_directory.resolve()
    manifest = json.loads((kernel/'npu-build-manifest.json').read_text())
    uki = json.loads((uki_dir/'uki-build-manifest.json').read_text())
    packages = json.loads((args.package_directory/'package-manifest.json').read_text())
    external = json.loads((args.external_directory/'external-module-build.json').read_text())
    if any(m.get('release', m.get('expected_release')) != RELEASE for m in (manifest, uki, packages, external)):
        parser.error('artifact release mismatch')
    if uki.get('valid') is False or digest(Path(uki['uki'])) != uki['sha256']:
        parser.error('UKI is rejected or its hash differs')
    verify(Path(uki['uki']), kernel/'arch/arm64/boot/Image', uki_dir/'cmdline')
    for path, pin in uki['configs'].items():
        if digest(Path(path)) != pin:
            parser.error('host initramfs configuration changed since UKI creation')
    for row in packages['packages']:
        if digest(Path(row['path'])) != row['sha256']:
            parser.error('package checksum mismatch')
        audit_package(Path(row['path']), RELEASE, manifest['image_sha256'], manifest['source_pins']['.config'])
    module_rows = external['modules']+[manifest['observation_module']]
    for row in module_rows:
        if digest(Path(row['path'])) != row['sha256'] or row['vermagic'].split()[0] != RELEASE:
            parser.error('prebuilt module checksum/release mismatch')
    modules = Path('/usr/lib/modules')/RELEASE
    target = Path('/boot/EFI/Linux')/EFI_NAME
    checkpoint = Path('/var/lib/n1x-npu-test/npu1')
    if modules.exists() or target.exists() or checkpoint.exists():
        parser.error('test installation or checkpoint already exists; inspect it rather than restarting')
    if shutil.disk_usage('/boot').free < Path(uki['uki']).stat().st_size+64*1024*1024:
        parser.error('insufficient EFI space')
    # The same lock is used by the existing P16 installer. No menu/package
    # mutation occurs before every artifact and current-menu check has passed.
    with Path('/run/lock/boot-partition.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if modules.exists() or target.exists() or checkpoint.exists():
            parser.error('test destination changed while acquiring the boot lock')
        menu = Path('/boot/limine.conf')
        before = menu.read_bytes()
        if hashlib.sha256(before).hexdigest() != uki['boot_menu_sha256']:
            parser.error('boot menu changed since the recorded snapshot')
        old = re.findall(r'boot\(\):(/EFI/Linux/omarchy_linux-p16-asus-23\.efi)#([a-f0-9]{128})', before.decode())
        if len(old) != 1 or hashlib.blake2b((Path('/boot')/old[0][0].lstrip('/')).read_bytes()).hexdigest() != old[0][1]:
            parser.error('existing default EFI hash is not valid')
        blake = hashlib.blake2b(Path(uki['uki']).read_bytes()).hexdigest()
        after = render_entry(before.decode(), blake, (uki_dir/'cmdline').read_text().strip()).encode()
        checkpoint.mkdir(mode=0o700, parents=True)
        (checkpoint/'limine.conf.before').write_bytes(before)
        state = {'release': RELEASE, 'stage': 'preflight-complete', 'uki_sha256': uki['sha256'],
                 'efi_path': str(target), 'default_unchanged': True,
                 'modules': [{'path': str(modules/'updates/npu-test'/Path(r['path']).name),
                              'sha256': r['sha256']} for r in module_rows]}
        def save(stage):
            state['stage'] = stage
            atomic_write(checkpoint/'state.json', (json.dumps(state, indent=2)+'\n').encode())
        save('preflight-complete')
        with tempfile.TemporaryDirectory(prefix='n1x-npu-hooks-', dir='/run') as tmp:
            hookdir = Path(tmp)
            for directory in (Path('/usr/share/libalpm/hooks'), Path('/etc/pacman.d/hooks')):
                for hook in directory.glob('*.hook'):
                    link = hookdir/hook.name
                    if not link.is_symlink():
                        link.symlink_to('/dev/null')
            subprocess.run(['pacman', '-U', '--noconfirm', '--hookdir', str(hookdir),
                            *[r['path'] for r in packages['packages']]], check=True)
        save('packages-installed')
        updates = modules/'updates/npu-test'
        updates.mkdir(parents=True)
        for row in module_rows:
            path = updates/Path(row['path']).name
            shutil.copyfile(row['path'], path)
            path.chmod(0o644)
            if digest(path) != row['sha256']:
                raise RuntimeError('installed module checksum mismatch')
        subprocess.run(['depmod', '-a', RELEASE], check=True)
        for name in ('nvidia', 'nvidia_drm', 'spbm', 'cpuid', 'spi_hid_acpi', 'hid_asus', 'n1x_npu_observe'):
            if subprocess.check_output(['modinfo', '-k', RELEASE, '-F', 'vermagic', name], text=True).split()[0] != RELEASE:
                raise RuntimeError('installed module release mismatch: '+name)
            dependencies = subprocess.check_output(['modprobe', '-S', RELEASE, '--show-depends', name], text=True)
            for line in dependencies.splitlines():
                if line.startswith('insmod ') and not Path(line.split()[1]).is_file():
                    raise RuntimeError('missing installed module dependency: '+line)
        save('modules-installed')
        atomic_write(target, Path(uki['uki']).read_bytes())
        if hashlib.blake2b(target.read_bytes()).hexdigest() != blake:
            raise RuntimeError('deployed EFI hash mismatch; menu not changed')
        save('efi-copied')
        if menu.read_bytes() != before:
            raise RuntimeError('menu changed during installation; new entry not written')
        try:
            atomic_write(menu, after)
            if menu.read_bytes() != after:
                raise RuntimeError('boot menu verification failed')
            os.sync()
        except BaseException:
            if menu.read_bytes() == after:
                atomic_write(menu, before)
            raise
        save('ready-for-manual-boot')
        print('Installed N1X NPU test entry; existing default/timeout preserved; no reboot', flush=True)
        print('Checkpoint:', checkpoint, flush=True)


if __name__ == '__main__':
    main()
