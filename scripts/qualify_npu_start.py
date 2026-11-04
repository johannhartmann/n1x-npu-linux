#!/usr/bin/env python3
"""Step-by-step N1X NPU startup qualifier for manual sysfs bring-up stages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from npu_trigger import trigger, TRIGGERS
from select_npu_dma_domains import plan, apply

ROOT = Path(__file__).resolve().parents[1]
ROM_SHA256 = 'a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c'
PHASES = (
    ('prepare_hfrp', 'power_accepted'), ('upload', 'idle_observed'),
    ('ping', 'ping_verified'), ('init_region5', 'region5_registered'),
    ('read_config', 'config_valid'), ('apply_config', 'config_apply_accepted'),
    ('apply_lpwr', 'lpwr_accepted'), ('bypass_task', 'task_verified'),
)


def fields(status):
    return dict(item.split('=', 1) for item in status.split() if '=' in item)


def phase_status_file(phase):
    return 'hfrp_status' if phase == 'prepare_hfrp' else 'status'


def preflight_accel(dev, record, accel_class=Path('/sys/class/accel')):
    # Exercise open, DRM VERSION and INFO before any firmware trigger.
    # Registration alone does not prove that the node can be opened.
    sys.path.insert(0, str(ROOT/'runtime'))
    from n1x_dla import Device
    nodes = [entry for entry in accel_class.glob('accel*')
             if (entry/'device').resolve() == dev.resolve()]
    if len(nodes) != 1:
        raise RuntimeError('expected one accel node for the N1X endpoint')
    with Device('/dev/accel/'+nodes[0].name) as device:
        record(action='accel ABI preflight', info=device.info())


def run_phases(dev, through, record, after_hfrp=False):
    if through not in {phase for phase, _ in PHASES}:
        raise ValueError('unknown final phase')
    if after_hfrp:
        hfrp, status = fields((dev/'hfrp_status').read_text()), fields((dev/'status').read_text())
        if (through == 'prepare_hfrp' or hfrp.get('power_accepted') != '1' or
                hfrp.get('configured') != '1' or hfrp.get('phase') != '5' or
                hfrp.get('error') != '0' or status.get('attempted') != '0'):
            raise ValueError('requires successful HFRP and no previous upload attempt')
    for phase, success in PHASES[1 if after_hfrp else 0:]:
        record(action='trigger', phase=phase)
        os.sync()
        try:
            trigger(dev/phase, TRIGGERS[phase])
        except OSError as exc:
            record(phase=phase, error=str(exc), errno=exc.errno,
                   hfrp=(dev/'hfrp_status').read_text(), status=(dev/'status').read_text(),
                   phase_status=(dev/phase_status_file(phase)).read_text())
            raise
        hfrp, status = (dev/'hfrp_status').read_text(), (dev/'status').read_text()
        current = (dev/phase_status_file(phase)).read_text()
        record(phase=phase, hfrp=hfrp, status=status, phase_status=current)
        if fields(current).get(success) != '1':
            raise RuntimeError(phase+' returned without expected status flag')
        if phase == through:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through', choices=[p[0] for p in PHASES], default='prepare_hfrp')
    parser.add_argument('--log', type=Path, required=True, help='new JSONL file, never overwritten')
    parser.add_argument('--after-hfrp', action='store_true',
                        help='continue this boot after verified HFRP; never repeat handshake/upload')
    parser.add_argument('--enable-accel', action='store_true',
                        help='register /dev/accel/accel* device node during fresh module load')
    parser.add_argument('--strict-platform', action='store_true',
                        help='enforce exact DMI/ROM-hash pins')
    args = parser.parse_args()
    if args.enable_accel and args.after_hfrp:
        parser.error('--enable-accel must be selected at fresh module load')
    running_kernel = platform.release()
    if os.geteuid():
        parser.error('requires root')
    loaded = Path('/sys/module/n1x_npu_bootstrap')
    if loaded.exists() != args.after_hfrp:
        parser.error('fresh start requires no module; --after-hfrp requires the existing module')
    profile = Path('/sys/firmware/acpi/platform_profile').read_text().strip()
    rows = [] if args.after_hfrp else plan()
    module = ROOT/'driver/n1x_npu_bootstrap.ko'
    rom = ROOT/'results/vbios.rom'
    image = rom.read_bytes() if rom.is_file() else None
    if args.strict_platform and (image is None or len(image) != 0x110000 or
                                 hashlib.sha256(image).hexdigest() != ROM_SHA256):
        parser.error('board ROM differs from the verified capture')
    vermagic = subprocess.check_output(['/usr/bin/modinfo', '-F', 'vermagic', str(module)], text=True)
    if vermagic.split()[0] != running_kernel:
        parser.error('module vermagic does not match the running kernel')
    if args.after_hfrp:
        built = subprocess.check_output(['/usr/bin/modinfo', '-F', 'srcversion', str(module)], text=True).strip()
        if not built or (loaded/'srcversion').read_text().strip() != built:
            parser.error('loaded module source version differs from the reviewed build')
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
               kernel=running_kernel, platform_profile=profile, module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),
               rom_sha256=hashlib.sha256(image).hexdigest() if image else 'live_vbios_carveout',
               through=args.through, after_hfrp=args.after_hfrp, groups=rows, enable_accel=args.enable_accel)
        dev = Path('/sys/bus/platform/devices/NVDA200A:00')
        if args.after_hfrp:
            if (dev/'driver').resolve().name != 'n1x-npu-bootstrap':
                raise RuntimeError('DLA is not bound to the bootstrap')
            run_phases(dev, args.through, record, after_hfrp=True)
            record(completed_through=args.through)
            return
        if image is not None and len(image) == 0x110000 and hashlib.sha256(image).hexdigest() == ROM_SHA256:
            destination = Path('/usr/lib/firmware/nvidia/n1x/vbios.rom')
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
        subprocess.run(['/usr/bin/insmod', str(module), 'autostart=0', 'debug_sysfs=1', 'enable_hfrp=1', 'enable_upload=1',
                        'enable_ping=1', 'enable_region5=1', 'enable_config_snapshot=1',
                        'enable_config_apply=1', 'enable_lpwr_config=1', 'enable_bypass_task=1',
                        'enable_accel='+str(int(args.enable_accel)),
                        'strict_platform='+str(int(args.strict_platform))], check=True)
        dev = Path('/sys/bus/platform/devices/NVDA200A:00')
        if (dev/'driver').resolve().name != 'n1x-npu-bootstrap':
            raise RuntimeError('DLA did not bind to the bootstrap')
        if args.enable_accel:
            preflight_accel(dev, record)
        run_phases(dev, args.through, record)
        record(completed_through=args.through, retained_until_checked_shutdown=True,
               general_inference_verified=False,
               automatic_start_installed=False)


if __name__ == '__main__':
    main()
