#!/usr/bin/env python3
"""Build the VM-qualified diagnostic source as a separate npu2 kernel release."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from build_sroot_kernel import ROOT, digest, build_observation_module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qualified_build', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    source = args.qualified_build.resolve()
    destination = args.destination.resolve()
    record = json.loads((source / 'bootlog-build.json').read_text())
    kernel = source / 'kernel'
    if not record.get('in_tree_modules_rebuilt_against_exact_vmlinux') or record['capture_enabled']:
        parser.error('requires the capture-OFF build with matching modules')
    if digest(kernel / 'arch/arm64/boot/Image') != record['kernel_sha256']:
        parser.error('source Image changed')
    if destination.exists() or not destination.is_relative_to(ROOT / 'results') or destination.is_relative_to(source):
        parser.error('destination must be new, under results, and outside source')
    config = digest(kernel / '.config')
    subprocess.run(['cp', '-a', '--reflink=auto', str(kernel), str(destination)], check=True)
    (destination / 'localversion.99-npu').write_text('-npu2\n')
    release = '7.2.8-23-omarchy-p16-asus-npu2'
    actual = subprocess.check_output(['make', '-s', 'kernelrelease'], cwd=destination, text=True).strip()
    if actual != release:
        raise RuntimeError('Unexpected kernel release: ' + actual)
    report = {'stage': 'building', 'expected_release': release, 'base_release': record['verification']['release'],
              'source': str(kernel), 'source_image_sha256': record['kernel_sha256'],
              'source_pins': {'.config': config}, 'installed': False, 'capture_enabled': False}
    manifest = destination / 'npu-build-manifest.json'
    manifest.write_text(json.dumps(report, indent=2) + '\n')
    subprocess.run(['make', '-j8', 'Image', 'modules'], cwd=destination, check=True)
    if digest(destination / '.config') != config:
        raise RuntimeError('Kernel configuration changed')
    old_module = destination / 'n1x-npu-module'
    if old_module.exists():
        shutil.rmtree(old_module)
    observation = build_observation_module(destination, release)
    report.update(stage='built', image_sha256=digest(destination / 'arch/arm64/boot/Image'),
                  observation_module=observation)
    manifest.write_text(json.dumps(report, indent=2) + '\n')
    print('Built isolated npu2 kernel and matching modules; nothing installed.')


if __name__ == '__main__':
    main()
