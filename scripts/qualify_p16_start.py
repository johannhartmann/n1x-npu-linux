#!/usr/bin/env python3
"""Run a bounded P16 startup prefix, with durable logs and no retries.

Explicit manual experiment on npu2, not an autostart service. Starts only
with unbound endpoints and no bootstrap loaded. Leaves attempted hardware
state/module/DMA retained until reboot, including after success.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess

from npu_trigger import trigger, TRIGGERS
from select_npu_dma_domains import plan, apply

ROOT = Path(__file__).resolve().parents[1]
KERNEL = '7.2.8-23-omarchy-p16-asus-npu2'
ROM_SHA256 = 'a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c'
PHASES = (
    ('prepare_hfrp', 'power_accepted'), ('upload', 'idle_observed'),
    ('ping', 'ping_verified'), ('init_region5', 'region5_registered'),
    ('read_config', 'config_valid'), ('apply_config', 'config_apply_accepted'),
    ('apply_lpwr', 'lpwr_accepted'), ('bypass_task', 'task_verified'),
)


def fields(status):
    return dict(item.split('=', 1) for item in status.split() if '=' in item)


def run_phases(dev, through, record):
    if through not in {phase for phase, _ in PHASES}:
        raise ValueError('unknown final phase')
    for phase, success in PHASES:
        record(action='trigger', phase=phase)
        os.sync()
        try:
            trigger(dev/phase, TRIGGERS[phase])
        except OSError as exc:
            record(phase=phase, error=str(exc), errno=exc.errno,
                   hfrp=(dev/'hfrp_status').read_text(), status=(dev/'status').read_text())
            raise
        hfrp, status = (dev/'hfrp_status').read_text(), (dev/'status').read_text()
        record(phase=phase, hfrp=hfrp, status=status)
        if fields(hfrp if phase == 'prepare_hfrp' else status).get(success) != '1':
            raise RuntimeError(phase+' returned without the required success evidence')
        if phase == through:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through', choices=[p[0] for p in PHASES], default='prepare_hfrp')
    parser.add_argument('--log', type=Path, required=True, help='new JSONL file, never overwritten')
    args = parser.parse_args()
    if os.geteuid() or platform.release() != KERNEL:
        parser.error('requires root on the audited npu2 kernel')
    if Path('/sys/module/n1x_npu_bootstrap').exists():
        parser.error('bootstrap already loaded; inspect retained state, do not repeat')
    if Path('/sys/firmware/acpi/platform_profile').read_text().strip() != 'performance':
        parser.error('requires the user-selected performance profile')
    rows = plan()  # All DMI, unbound device, group isolation and SMMU checks.
    module = ROOT/'driver/n1x_npu_bootstrap.ko'
    rom = ROOT/'results/p16-bios300-physical.rom'
    image = rom.read_bytes()
    if len(image) != 0x110000 or hashlib.sha256(image).hexdigest() != ROM_SHA256:
        parser.error('board ROM differs from the verified capture')
    vermagic = subprocess.check_output(['/usr/bin/modinfo', '-F', 'vermagic', str(module)], text=True)
    if vermagic.split()[0] != KERNEL:
        parser.error('module vermagic does not match the running test kernel')
    with args.log.open('x', buffering=1) as log:
        os.fchmod(log.fileno(), 0o600)
        if 'PKEXEC_UID' in os.environ:
            os.fchown(log.fileno(), int(os.environ['PKEXEC_UID']), -1)

        def record(**data):
            line = json.dumps(data)
            print(line, flush=True)
            print(line, file=log, flush=True)
            os.fsync(log.fileno())

        record(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               kernel=KERNEL, module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),
               rom_sha256=ROM_SHA256, through=args.through, groups=rows)
        destination = Path('/usr/lib/firmware/nvidia/n1x/p16-bios300-physical.rom')
        if destination.exists():
            if destination.read_bytes() != image:
                raise RuntimeError('installed board ROM differs; not overwritten')
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(image)
                out.flush()
                os.fsync(out.fileno())
        record(action='select dedicated DMA domains')
        apply(rows)
        record(action='load manual bootstrap, all triggers opt-in')
        subprocess.run(['/usr/bin/insmod', str(module), 'enable_hfrp=1', 'enable_upload=1',
                        'enable_ping=1', 'enable_region5=1', 'enable_config_snapshot=1',
                        'enable_config_apply=1', 'enable_lpwr_config=1', 'enable_bypass_task=1'], check=True)
        dev = Path('/sys/bus/platform/devices/NVDA200A:00')
        if (dev/'driver').resolve().name != 'n1x-npu-bootstrap':
            raise RuntimeError('DLA did not bind to the bootstrap')
        run_phases(dev, args.through, record)
        record(completed_through=args.through, retained_until_reboot=True,
               inference_verified=False, automatic_start_installed=False)


if __name__ == '__main__':
    main()
