#!/usr/bin/env python3
"""Stage a capture-OFF diagnostic UKI with bounded /data logging; never deploy."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from build_sroot_kernel import digest
from verify_sroot_uki import verify, RELEASE
from package_sroot_kernel import package_base

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('diagnostic', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build, diagnostic, output = args.build.resolve(), args.diagnostic.resolve(), args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / 'results'):
        parser.error('Output must be new and under repository results')
    if output.is_relative_to(build) or output.is_relative_to(diagnostic):
        parser.error('Output must be outside source directories')
    record = json.loads((build / 'frts-build.json').read_text())
    prior = json.loads((diagnostic / 'diagnostic.json').read_text())
    if digest(diagnostic / 'frts-diagnostic.efi') != prior['sha256']:
        parser.error('Diagnostic source changed')
    cmdline = (diagnostic / 'cmdline').read_text().strip()
    if 'n1x_frts_snapshot' in cmdline or 'keep_bootcon' not in cmdline.split() or 'n1x_bootlog' in cmdline:
        parser.error('Requires capture-OFF, keep-console baseline without bootlog')
    kernel = build / 'kernel/arch/arm64/boot/Image'
    if digest(kernel) != record['image_sha256']:
        parser.error('Kernel Image changed')
    verify(diagnostic / 'frts-diagnostic.efi', kernel, diagnostic / 'cmdline')
    subprocess.run(['cp', '-a', '--reflink=auto', str(build / 'uki'), str(output)], check=True)
    for name in ('linux-p16-asus-23-npu1.efi', 'boot-menu-before.txt', 'mkinitcpio.log'):
        (output / name).unlink(missing_ok=True)
    (output / 'cmdline').write_text(cmdline + ' n1x_bootlog=1\n')
    # Split module BTF refers to this exact vmlinux's type IDs. Matching symbol
    # CRCs alone does not make modules copied from the base kernel loadable.
    rebuilt = output / 'kernel'
    subprocess.run(['cp', '-a', '--reflink=auto', str(build / 'kernel'), str(rebuilt)], check=True)
    with (output / 'modules-build.log').open('w') as log:
        subprocess.run(['make', '-j8', 'Image', 'modules'], cwd=rebuilt,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(['make', '-j8', 'modules_install', 'INSTALL_MOD_STRIP=1',
                        'DEPMOD=/bin/true', 'INSTALL_MOD_PATH=' + str(output / 'root')],
                       cwd=rebuilt, stdout=log, stderr=subprocess.STDOUT, check=True)
    subprocess.run(['depmod', '-b', str(output / 'root'), RELEASE], check=True)
    kernel = rebuilt / 'arch/arm64/boot/Image'
    shutil.copy2(kernel, output / 'root/usr/lib/modules' / RELEASE / 'vmlinuz')
    finish(output, kernel)


def finish(output, kernel, release=RELEASE):
    """Pack and verify a staged matching kernel/module root; never deploy it."""
    hooks = output / 'initcpio'
    (hooks / 'hooks').mkdir(exist_ok=True)
    for subdir in ('hooks', 'install'):
        shutil.copy2(ROOT / 'initcpio' / subdir / 'n1x-bootlog', hooks / subdir / 'n1x-bootlog')
        (hooks / subdir / 'n1x-bootlog').chmod(0o755)
    subprocess.run(['cc', '-static', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(ROOT / 'scripts/n1x_bootlog.c'), '-o', str(hooks / 'n1x-bootlog')], check=True)
    with (output / 'mkinitcpio.conf').open('a') as config:
        config.write('''
# Run the disk logger after udev starts, before normal MODULES (including GPU).
_bootlog_hooks=()
_bootlog_inserted=0
for _bootlog_hook in "${HOOKS[@]}"; do
    _bootlog_hooks+=("$_bootlog_hook")
    if [[ $_bootlog_hook == udev ]]; then
        _bootlog_hooks+=(n1x-bootlog)
        _bootlog_inserted=1
    fi
done
[[ $_bootlog_inserted == 1 ]] || { echo 'n1x-bootlog requires udev initramfs' >&2; return 1; }
HOOKS=("${_bootlog_hooks[@]}")
unset _bootlog_hooks _bootlog_hook _bootlog_inserted
''')
    with (output / 'mkinitcpio.log').open('w') as log:
        subprocess.run(['pkexec', '/usr/bin/python3', str(ROOT / 'scripts/run_sroot_mkinitcpio.py'),
                        str(output), '--release', release], stdout=log, stderr=subprocess.STDOUT, check=True)
    if '==> ERROR:' in (output / 'mkinitcpio.log').read_text():
        raise RuntimeError('mkinitcpio reported an error')
    image = output / (package_base(release) + '.efi')
    checked = verify(image, kernel, output / 'cmdline', release)
    with tempfile.TemporaryDirectory(prefix='n1x-bootlog-check-') as temporary:
        extracted = Path(temporary)
        subprocess.run(['lsinitcpio', '--extract', str(image)], cwd=extracted,
                       stdout=subprocess.DEVNULL, check=True)
        config = (extracted / 'config').read_text()
        if not any(line.startswith('EARLYHOOKS=') and 'udev n1x-bootlog' in line for line in config.splitlines()):
            raise RuntimeError('Logger not placed immediately after udev early hook')
        if digest(extracted / 'usr/bin/n1x-bootlog') != digest(hooks / 'n1x-bootlog'):
            raise RuntimeError('Recorder binary mismatch')
        if (extracted / 'hooks/n1x-bootlog').read_bytes() != (hooks / 'hooks/n1x-bootlog').read_bytes():
            raise RuntimeError('Recorder hook mismatch')
    (output / 'bootlog-build.json').write_text(json.dumps({
        'sha256': digest(image), 'kernel_sha256': digest(kernel), 'verification': checked,
        'capture_enabled': False, 'earlyhook_order_verified': True,
        'recorder_sha256': digest(hooks / 'n1x-bootlog'),
        'deployed': False, 'physical_initramfs_run_verified': False,
        'in_tree_modules_rebuilt_against_exact_vmlinux': True,
        'coverage': 'From initramfs earlyhook, includes available kernel backlog; no pre-initramfs guarantee',
    }, indent=2) + '\n')
    print('Built logged initramfs outside /boot. Physical boot qualification remains pending.')


if __name__ == '__main__':
    main()
