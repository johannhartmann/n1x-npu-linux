#!/usr/bin/env python3
"""Build installed P16 external-driver versions for the isolated NPU test kernel.

Copies /usr/src inputs and never installs modules or updates DKMS/boot state.
"""
import argparse
import json
from pathlib import Path
import subprocess
from build_sroot_kernel import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--jobs', type=int, default=8)
    args = parser.parse_args()
    kernel, destination = args.kernel.resolve(), args.destination.absolute()
    manifest = json.loads((kernel/'npu-build-manifest.json').read_text())
    release = manifest['expected_release']
    if manifest['stage'] != 'built' or release != '7.2.8-23-omarchy-p16-asus-npu1':
        parser.error('requires the completed isolated NPU test-kernel build')
    if digest(kernel/'arch/arm64/boot/Image') != manifest['image_sha256']:
        parser.error('kernel Image no longer matches the completed build')
    if destination.exists() or destination.is_symlink():
        parser.error('destination must be new')
    if destination.resolve().is_relative_to(Path('/usr/src')):
        parser.error('destination must be outside installed driver sources')
    if not 1 <= args.jobs <= 32:
        parser.error('jobs must be 1..32')
    sources = ['nvidia-615.78.08', 'spbm-0.3.0.rtxspark.5', 'cpuid-0.8.2']
    for name in sources:
        if not (Path('/usr/src')/name/'dkms.conf').is_file():
            parser.error('missing installed source '+name)
    destination.mkdir()
    report = {'release': release, 'installed': False, 'stage': 'building', 'modules': []}
    record = destination/'external-module-build.json'
    record.write_text(json.dumps(report, indent=2)+'\n')
    for name in sources:
        source = Path('/usr/src')/name
        target = destination/name
        subprocess.run(['cp', '-a', '--reflink=auto', str(source), str(target)], check=True)
        if name.startswith('nvidia-'):
            subprocess.run(['make', f'-j{args.jobs}', 'IGNORE_PREEMPT_RT_PRESENCE=1',
                            'NV_EXCLUDE_BUILD_MODULES=', 'SYSSRC='+str(kernel),
                            'SYSOUT='+str(kernel), 'KERNEL_UNAME='+release, 'modules'],
                           cwd=target, check=True)
        else:
            subprocess.run(['make', f'-j{args.jobs}', '-C', str(kernel), 'M='+str(target), 'modules'],
                           check=True)
        for module in sorted(target.rglob('*.ko')):
            vermagic = subprocess.check_output(['modinfo', '-F', 'vermagic', str(module)], text=True).strip()
            if vermagic.split()[0] != release:
                raise RuntimeError('module has incorrect kernel release: '+str(module))
            report['modules'].append(dict(path=str(module), sha256=digest(module), vermagic=vermagic,
                                          source_package=name, dkms_config_sha256=digest(source/'dkms.conf')))
        record.write_text(json.dumps(report, indent=2)+'\n')
    expected = {'nvidia.ko', 'nvidia-drm.ko', 'nvidia-modeset.ko', 'nvidia-uvm.ko',
                'nvidia-peermem.ko', 'spbm.ko', 'cpuid.ko'}
    if {Path(m['path']).name for m in report['modules']} != expected:
        raise RuntimeError('external module inventory differs from expected installed drivers')
    report['stage'] = 'built'
    record.write_text(json.dumps(report, indent=2)+'\n')
    print('Built seven external modules for', release, '; none installed', flush=True)


if __name__ == '__main__':
    main()
