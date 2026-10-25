#!/usr/bin/env python3
"""Privileged, staging-only mkinitcpio execution; does not install or deploy."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RELEASE = '7.2.8-23-omarchy-p16-asus-npu1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    output = args.output.resolve(strict=True)
    if os.geteuid() != 0:
        parser.error('requires root for real initramfs metadata')
    if not output.is_relative_to((ROOT/'results').resolve()):
        parser.error('output must be a private build directory under repository results')
    owner = output.stat()
    menu = output/'boot-menu-before.txt'
    menu.write_bytes(Path('/boot/limine.conf').read_bytes())
    menu.chmod(0o600)
    os.chown(menu, owner.st_uid, owner.st_gid)
    image = output/'linux-p16-asus-23-npu1.efi'
    command = ['mkinitcpio', '--nocolor', '--nopost', '--kernel', RELEASE,
               '--moduleroot', str(output/'root'), '--config', str(output/'mkinitcpio.conf'),
               '--hookdir', str(output/'initcpio'), '--hookdir', '/etc/initcpio',
               '--hookdir', '/usr/lib/initcpio', '--cmdline', str(output/'cmdline'),
               '--uki', str(image)]
    try:
        subprocess.run(command, check=True)
    finally:
        if image.is_file():
            os.chown(image, owner.st_uid, owner.st_gid)
            image.chmod(0o600)


if __name__ == '__main__':
    main()
