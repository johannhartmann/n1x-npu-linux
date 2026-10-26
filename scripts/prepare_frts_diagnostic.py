#!/usr/bin/env python3
"""Make a verbose UKI with capture disabled; preserve every other PE section.

Run with the research venv (pefile). This only writes a new staging directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pefile
from verify_sroot_uki import verify


def diagnostic_cmdline(text):
    # Preserve quoting of acpi_osi="!Windows 2022" and all root/device options.
    text = re.sub(r'(?<!\S)(?:quiet|splash|loglevel=\S+|systemd.show_status=\S+|'
                  r'rd.udev.log_level=\S+|n1x_frts_snapshot=\S+)(?!\S)', '', text.strip())
    return re.sub(' +', ' ', text).strip() + (
        ' earlycon=efifb keep_bootcon loglevel=7 ignore_loglevel initcall_debug'
        ' systemd.show_status=true rd.udev.log_level=info plymouth.enable=0 panic=0')


def replace_cmdline(source, target, cmdline):
    before = pefile.PE(str(source))
    sections = sorted(before.sections, key=lambda s: s.VirtualAddress)
    index = next(i for i, s in enumerate(sections) if s.Name.rstrip(b'\0') == b'.cmdline')
    data = cmdline.read_bytes()
    if len(data) >= sections[index + 1].VirtualAddress - sections[index].VirtualAddress:
        raise ValueError('Command line exceeds the existing PE section gap')
    if before.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size:
        raise ValueError('Refusing to invalidate a signed image')
    subprocess.run(['objcopy', '--update-section', '.cmdline=' + str(cmdline),
                    str(source), str(target)], check=True)
    after = pefile.PE(str(target))
    # BFD retains the old VirtualSize on PE --update-section. The EFI stub
    # uses that size, so update it explicitly and regenerate the checksum.
    changed = next(s for s in after.sections if s.Name.rstrip(b'\0') == b'.cmdline')
    changed.Misc_VirtualSize = len(data)
    after.OPTIONAL_HEADER.CheckSum = after.generate_checksum()
    after.write(str(target))
    after.close()
    after = pefile.PE(str(target))
    if len(before.sections) != len(after.sections):
        raise ValueError('PE section count changed')
    for old, new in zip(before.sections, after.sections):
        if old.Name != new.Name or old.VirtualAddress != new.VirtualAddress:
            raise ValueError('PE section name/address changed')
        if old.Name.rstrip(b'\0') != b'.cmdline' and old.get_data() != new.get_data():
            raise ValueError('Non-command-line section changed: ' + repr(old.Name))
    if before.OPTIONAL_HEADER.AddressOfEntryPoint != after.OPTIONAL_HEADER.AddressOfEntryPoint:
        raise ValueError('EFI entry point changed')
    before.close()
    after.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    report = json.loads((args.build / 'frts-build.json').read_text())
    source = args.build / 'uki/linux-p16-asus-23-npu1.efi'
    if hashlib.sha256(source.read_bytes()).hexdigest() != report['uki_sha256']:
        parser.error('Source UKI changed')
    args.output.mkdir()
    cmdline = args.output / 'cmdline'
    cmdline.write_text(diagnostic_cmdline((args.build / 'uki/cmdline').read_text()) + '\n')
    image = args.output / 'frts-diagnostic.efi'
    replace_cmdline(source, image, cmdline)
    checked = verify(image, args.build / 'kernel/arch/arm64/boot/Image', cmdline)
    (args.output / 'diagnostic.json').write_text(json.dumps({
        'original_sha256': report['uki_sha256'], 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
        'capture_enabled': False, 'only_cmdline_changed': True, 'verification': checked,
    }, indent=2) + '\n')
    print('Verified diagnostic UKI: capture disabled, early EFI console enabled.')


if __name__ == '__main__':
    main()
