#!/usr/bin/env python3
"""Enable the bounded snapshot on the physically booted npu2 image; stage only."""
import argparse
import json
from pathlib import Path

from build_sroot_kernel import digest
from verify_sroot_uki import verify

ROOT = Path(__file__).resolve().parents[1]
RELEASE = '7.2.8-23-omarchy-p16-asus-npu2'
PARAMETER = 'n1x_frts_snapshot=p16-bios300'


def capture_cmdline(original):
    options = original.strip().split()
    if any(p.startswith(('n1x_frts_snapshot', 'log_buf_len=')) for p in options):
        raise ValueError('Source already contains capture or log-buffer options')
    if not {'keep_bootcon', 'earlycon=efifb', 'n1x_bootlog=1'} <= set(options):
        raise ValueError('Requires the qualified EFI console and SSD logger')
    # Keep original quoting, in particular acpi_osi="!Windows 2022".
    return original.strip() + ' log_buf_len=4M ' + PARAMETER


def main():
    from prepare_frts_diagnostic import replace_cmdline
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('uki', type=Path)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    base, kernel, output = (p.resolve() for p in (args.uki, args.kernel, args.output))
    if output.exists() or not output.is_relative_to(ROOT / 'results'):
        parser.error('Output must be new and under results')
    record = json.loads((base / 'uki-build-manifest.json').read_text())
    source = base / 'linux-p16-asus-23-npu2.efi'
    if record['release'] != RELEASE or digest(source) != record['sha256']:
        parser.error('Base image does not match npu2 manifest')
    image = kernel / 'arch/arm64/boot/Image'
    verify(source, image, base / 'cmdline', RELEASE)
    output.mkdir()
    (output / 'cmdline').write_text(capture_cmdline((base / 'cmdline').read_text()) + '\n')
    target = output / 'frts-capture.efi'
    replace_cmdline(source, target, output / 'cmdline')
    checked = verify(target, image, output / 'cmdline', RELEASE)
    (output / 'capture.json').write_text(json.dumps({
        'release': RELEASE, 'sha256': digest(target), 'base_sha256': digest(source),
        'kernel_sha256': digest(image), 'only_cmdline_changed': True,
        'verification': checked, 'capture_enabled': True, 'log_buf_len': '4M',
        'physical_capture_verified': False, 'deployed': False,
    }, indent=2) + '\n')
    print('Prepared npu2 capture UKI; identical kernel/initramfs, only command line changed.')


if __name__ == '__main__':
    main()
