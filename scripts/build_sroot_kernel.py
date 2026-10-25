#!/usr/bin/env python3
"""Build an isolated P16 SROOT-DMA test kernel; never install or modify boot state.

Requires an already built source tree matching the running kernel's config and
Image. Copies the tree before applying the narrowly scoped experimental patch.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()



def build_observation_module(kernel, expected_release):
    """Build from copied C sources so the running-kernel module is preserved."""
    module_root = kernel/'n1x-npu-module'
    module_root.mkdir()
    shutil.copytree(ROOT/'include', module_root/'include')
    (module_root/'driver').mkdir()
    for source in [ROOT/'driver/Kbuild', *sorted((ROOT/'driver').glob('*.c'))]:
        shutil.copy2(source, module_root/'driver'/source.name)
    subprocess.run(['make', '-C', str(kernel), 'M='+str(module_root/'driver'), 'modules'], check=True)
    module = module_root/'driver/n1x_npu_observe.ko'
    vermagic = subprocess.check_output(['modinfo', '-F', 'vermagic', str(module)], text=True).strip()
    if vermagic.split()[0] != expected_release:
        raise RuntimeError('observation module targets an unexpected kernel')
    return dict(path=str(module), sha256=digest(module), vermagic=vermagic,
                source_pins={str(path.relative_to(module_root)): digest(path)
                             for path in sorted(module_root.rglob('*'))
                             if path.is_file() and (path.suffix in ('.c', '.h') or path.name == 'Kbuild')
                             and not path.name.endswith('.mod.c')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--jobs', type=int, default=8)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.absolute()
    release = platform.release()
    if release != '7.2.8-23-omarchy-p16-asus':
        parser.error('this experiment is pinned to the audited running P16 kernel')
    if not 1 <= args.jobs <= 32:
        parser.error('jobs must be 1..32')
    if destination.resolve().is_relative_to(source):
        parser.error('destination must be outside the known-working source tree')
    if destination.exists() or destination.is_symlink():
        parser.error('destination must be new; an interrupted build must be inspected explicitly')
    installed = Path('/usr/lib/modules')/release
    pins = {}
    for relative, reference in (('.config', installed/'build/.config'),
                                ('arch/arm64/boot/Image', installed/'vmlinuz')):
        actual = digest(source/relative)
        if actual != digest(reference):
            parser.error(f'source {relative} does not match the running kernel package')
        pins[relative] = actual
    scan_pin = '6d347c65a1c995907f97a3cee50597f2155dd494ae648c0936c3dfb4bac43c26'
    if digest(source/'drivers/acpi/scan.c') != scan_pin:
        parser.error('ACPI source differs from the reviewed running-kernel base')
    pins['drivers/acpi/scan.c'] = scan_pin
    patch = ROOT/'patches/linux-7.2.8-p16-sroot-noncoherent-dma.patch'
    expected = '7.2.8-23-omarchy-p16-asus-npu1'
    # Ordinary copy fallback is intentional: hard-linked objects could corrupt
    # the known-working source/build when make updates its outputs.
    print('Copying verified source/build into', destination, flush=True)
    subprocess.run(['cp', '-a', '--reflink=auto', str(source), str(destination)], check=True)
    manifest = dict(base_release=release, expected_release=expected, source=str(source),
                    source_pins=pins, patch_sha256=digest(patch), installed=False,
                    stage='source-copied')
    report = destination/'npu-build-manifest.json'
    report.write_text(json.dumps(manifest, indent=2)+'\n')
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)],
                   cwd=destination, check=True)
    (destination/'localversion.99-npu').write_text('-npu1\n')
    actual_release = subprocess.check_output(['make', '-s', 'kernelrelease'],
                                            cwd=destination, text=True).strip()
    if actual_release != expected:
        raise RuntimeError(f'unexpected test release: {actual_release!r}')
    manifest['stage'] = 'building'
    report.write_text(json.dumps(manifest, indent=2)+'\n')
    subprocess.run(['make', f'-j{args.jobs}', 'Image', 'modules'], cwd=destination, check=True)
    # The source config must remain the exact running config; the release suffix
    # belongs in localversion, not in a replacement configuration.
    if digest(destination/'.config') != pins['.config']:
        raise RuntimeError('kernel configuration changed during build; inspect before use')
    module = build_observation_module(destination, expected)
    manifest.update(stage='built', image_sha256=digest(destination/'arch/arm64/boot/Image'),
                    observation_module=module)
    report.write_text(json.dumps(manifest, indent=2)+'\n')
    print('Built', actual_release, '; no packages, modules or boot entries installed', flush=True)


if __name__ == '__main__':
    main()
