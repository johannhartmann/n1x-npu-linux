#!/usr/bin/env python3
"""Check the actual test UKI kernel, command line, early ACPI and boot modules."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

RELEASE = '7.2.8-23-omarchy-p16-asus-npu1'
PINS = {'DSDT.aml': '80e0ad4a051704003d09029cb600a9814e1cd370ce774b9749dc09d2232b5aaf',
        'SSDT.aml': '9b28ffc14c19f8b7ea2278de88ff9ec070f5f70401107d81fef067a916aa9695'}


def early_files(data):
    offset, files = 0, {}
    while data[offset:offset+6] in (b'070701', b'070702'):
        if offset+110 > len(data):
            raise ValueError('truncated early CPIO header')
        fields = [int(data[offset+6+i*8:offset+14+i*8], 16) for i in range(13)]
        size, namesize = fields[6], fields[11]
        start = offset+110
        if namesize < 1 or start+namesize > len(data) or data[start+namesize-1] != 0:
            raise ValueError('invalid early CPIO filename')
        name = data[start:start+namesize-1].decode().removeprefix('./')
        start = (start+namesize+3)&~3
        if start+size > len(data):
            raise ValueError('truncated early CPIO data')
        if name in files:
            raise ValueError('duplicate early CPIO entry '+name)
        files[name] = data[start:start+size]
        offset = (start+size+3)&~3
        if name == 'TRAILER!!!':
            return files
    raise ValueError('early CPIO trailer missing')


def verify(image, kernel_image, cmdline, release=RELEASE):
    with tempfile.TemporaryDirectory(prefix='n1x-uki-check-') as tmp:
        tmp = Path(tmp)
        sections = ('.uname', '.linux', '.cmdline', '.initrd')
        args = ['objcopy']
        for section in sections:
            args += ['--dump-section', section+'='+str(tmp/section[1:])]
        # Explicit output stays in the temporary directory: never let objcopy
        # rewrite the input UKI while extracting its sections.
        args += [str(image), str(tmp/'copy.efi')]
        subprocess.run(args, check=True)
        if (tmp/'uname').read_bytes().rstrip(b'\0\n').decode() != release:
            raise ValueError('UKI kernel release mismatch')
        actual, expected = (tmp/'linux').read_bytes(), kernel_image.read_bytes()
        if actual[:len(expected)] != expected or any(actual[len(expected):]):
            raise ValueError('UKI kernel Image mismatch')
        if (tmp/'cmdline').read_bytes().rstrip(b'\0\n ').decode() != cmdline.read_text().strip():
            raise ValueError('UKI command line mismatch')
        early = early_files((tmp/'initrd').read_bytes())
        for name, pin in PINS.items():
            if hashlib.sha256(early['kernel/firmware/acpi/'+name]).hexdigest() != pin:
                raise ValueError('UKI ACPI override mismatch '+name)
        listing = subprocess.check_output(['lsinitcpio', '--list', str(tmp/'initrd')], text=True)
        for name in ('nvidia.ko', 'nvidia-modeset.ko', 'nvidia-drm.ko', 'nvidia-uvm.ko',
                     'spi-hid-acpi.ko', 'hid-asus.ko', 'i2c-mt65xx.ko', 'i2c-hid-acpi.ko'):
            if '/'+name not in listing:
                raise ValueError('UKI module missing '+name)
        for name in ('init', 'hooks/encrypt', 'usr/bin/cryptsetup', 'etc/vconsole.conf'):
            if name not in listing.splitlines():
                raise ValueError('UKI boot file missing '+name)
        if 'n1x_npu_observe.ko' in listing:
            raise ValueError('observation module must not bind devices from the initramfs')
    return {'release': release, 'kernel_image_matches': True, 'cmdline_matches': True,
            'acpi_matches': True, 'required_modules_and_boot_files': True,
            'physical_boot_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('kernel_image', type=Path)
    parser.add_argument('cmdline', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.image, args.kernel_image, args.cmdline), indent=2))
