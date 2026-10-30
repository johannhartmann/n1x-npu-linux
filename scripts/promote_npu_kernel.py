#!/usr/bin/env python3
"""Promote the qualified npu2 build to the stable co-installable NPU release."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from build_sroot_kernel import ROOT, build_observation_module, digest


SOURCE_RELEASE = '7.2.8-23-omarchy-p16-asus-npu2'
TARGET_RELEASE = '7.2.8-23-rtx-spark-npu'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qualified_kernel', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--jobs', type=int, default=8)
    args = parser.parse_args()
    source = args.qualified_kernel.resolve()
    destination = args.destination.resolve()
    if not 1 <= args.jobs <= 32:
        parser.error('jobs must be 1..32')
    if destination.exists() or not destination.is_relative_to(ROOT / 'results'):
        parser.error('destination must be new and under results')
    manifest = json.loads((source / 'npu-build-manifest.json').read_text())
    if manifest.get('stage') != 'built' or manifest.get('expected_release') != SOURCE_RELEASE:
        parser.error('source must be the completed, qualified npu2 kernel build')
    image = source / 'arch/arm64/boot/Image'
    if digest(image) != manifest.get('image_sha256'):
        parser.error('qualified source Image differs from its manifest')
    config_pin = digest(source / '.config')
    subprocess.run(
        ['cp', '-a', '--reflink=always', str(source), str(destination)], check=True,
    )
    for name in ('localversion.20-pkgname', 'localversion.30-p16', 'localversion.40-asus'):
        path = destination / name
        if not path.is_file():
            raise RuntimeError('qualified source is missing ' + name)
        path.unlink()
    (destination / 'localversion.30-rtx-spark').write_text('-rtx-spark\n')
    (destination / 'localversion.99-npu').write_text('-npu\n')
    actual = subprocess.check_output(
        ['make', '-s', 'kernelrelease'], cwd=destination, text=True,
    ).strip()
    if actual != TARGET_RELEASE:
        raise RuntimeError('unexpected promoted kernel release: ' + actual)
    report = {
        'stage': 'building',
        'expected_release': TARGET_RELEASE,
        'promoted_from_release': SOURCE_RELEASE,
        'source': str(source),
        'source_image_sha256': manifest['image_sha256'],
        'source_pins': {'.config': config_pin},
        'installed': False,
        'capture_enabled': False,
    }
    output = destination / 'npu-build-manifest.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    subprocess.run(
        ['make', f'-j{args.jobs}', 'Image', 'modules'], cwd=destination, check=True,
    )
    if digest(destination / '.config') != config_pin:
        raise RuntimeError('kernel configuration changed during promotion')
    for relative in (destination / 'modules.order').read_text().splitlines():
        # Kbuild records linked module objects in modules.order; the loadable
        # artifact carrying vermagic has the corresponding .ko suffix.
        module = (destination / relative).with_suffix('.ko')
        if not module.is_file():
            raise RuntimeError(f'promoted module artifact is missing: {module}')
        vermagic = subprocess.check_output(
            ['modinfo', '-F', 'vermagic', str(module)], text=True,
        ).split()[0]
        if vermagic != TARGET_RELEASE:
            raise RuntimeError(f'promoted module has stale release: {module}: {vermagic}')
    old_module = destination / 'n1x-npu-module'
    if old_module.exists():
        shutil.rmtree(old_module)
    observation = build_observation_module(destination, TARGET_RELEASE)
    report.update(
        stage='built',
        image_sha256=digest(destination / 'arch/arm64/boot/Image'),
        observation_module=observation,
    )
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Promoted qualified kernel to {TARGET_RELEASE}; nothing installed.')


if __name__ == '__main__':
    main()
