#!/usr/bin/env python3
"""Exercise the real diagnostic initramfs with an isolated file-backed NVMe."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from build_sroot_kernel import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('firmware', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    image, kernel, firmware = (p.resolve(strict=True) for p in (args.image, args.kernel, args.firmware))
    output = args.output.resolve()
    output.mkdir()  # Refuse to overwrite an earlier test or disk.
    disk = output / 'nvme.ext4'
    with disk.open('xb') as stream:
        stream.truncate(128 * 1024 * 1024)
    subprocess.run(['mkfs.ext4', '-q', '-U', 'e4b5f9f3-205f-469f-a75d-a09a67786a92', str(disk)], check=True)
    initrd = output / 'initrd'
    subprocess.run(['objcopy', '--dump-section', '.initrd=' + str(initrd),
                    str(image), str(output / 'copy.efi')], check=True)
    (output / 'copy.efi').unlink()
    command = ['qemu-system-aarch64', '-machine', 'virt,accel=tcg,gic-version=3',
               '-cpu', 'max', '-m', '4G', '-smp', '1', '-nographic', '-nodefaults',
               '-serial', 'mon:stdio', '-nic', 'none', '-no-reboot', '-bios', str(firmware),
               '-kernel', str(kernel), '-initrd', str(initrd),
               '-drive', f'if=none,id=logdisk,format=raw,file={disk}',
               '-device', 'nvme,drive=logdisk,serial=N1XLOGTEST',
               '-append', 'console=ttyAMA0 earlycon=pl011,0x9000000 loglevel=7 nokaslr '
                          'n1x_bootlog=1 break=premount root=/dev/missing plymouth.enable=0 panic=0']
    serial = output / 'serial.log'
    with serial.open('w') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while 'Pre-mount break requested' not in serial.read_text(errors='replace'):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('VM did not reach initramfs pre-mount shell; inspect serial.log')
                time.sleep(0.5)
            process.stdin.write(b'echo N1X_BOOTLOG_VM_PROBE >/dev/kmsg\nsleep 2\n'
                                b'. /hooks/n1x-bootlog\nrun_cleanuphook\npoweroff -f\n')
            process.stdin.flush()
            if process.wait(timeout=30):
                raise RuntimeError('VM did not shut down cleanly')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    recovered = output / 'recovered'
    if 'failed to validate module' in serial.read_text(errors='replace'):
        raise RuntimeError('Kernel rejected module BTF; do not deploy this image')
    recovered.mkdir()
    subprocess.run(['debugfs', '-R', f'rdump /n1x-bootlogs {recovered}', str(disk)], check=True)
    logs = list(recovered.rglob('*.log'))
    if len(logs) != 1:
        raise RuntimeError('Expected one persistent boot log')
    contents = logs[0].read_text()
    for marker in ('N1X_BOOTLOG_START', 'Linux version', 'N1X_BOOTLOG_VM_PROBE', 'N1X_BOOTLOG_END'):
        if marker not in contents:
            raise RuntimeError('Disk log missing ' + marker)
    if 'error=0' not in contents:
        raise RuntimeError('Recorder reported an error')
    report = {'uki_sha256': digest(image), 'kernel_sha256': digest(kernel),
              'firmware_sha256': digest(firmware), 'persistent_guest_log': str(logs[0]),
              'log_bytes': logs[0].stat().st_size, 'initramfs_nvme_write_and_recovery_passed': True,
              'physical_p16_boot_verified': False, 'pre_initramfs_hang_coverage': False}
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
