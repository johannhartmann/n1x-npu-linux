#!/usr/bin/env python3
"""Physically qualify stop -> DMA release -> unload -> load -> cuDLA inference.

Run as root on the audited npu2 kernel. Requires the pinned artifacts described
in docs/cudla-backend.md. Stops at the first failure; never forces unload,
resets a failed channel or retries an uncertain operation. Leaves the final
successfully reloaded device running, with durable per-cycle evidence.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from npu_trigger import trigger
from qualify_p16_start import KERNEL, fields

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path('/sys/module/n1x_npu_bootstrap')
ENDPOINT = Path('/sys/bus/platform/devices/NVDA200A:00')
SROOT = Path('/sys/bus/platform/devices/NVDA8600:00')
NODE = Path('/dev/accel/accel0')
sys.path.insert(0, str(ROOT/'runtime'))
from n1x_dla import Device


def stop_and_unload(record):
    record(action='stop requested', automatic_retry=False)
    os.sync()
    trigger(ENDPOINT/'shutdown', b'stop\n')
    state = fields((ENDPOINT/'shutdown_status').read_text())
    if any(state.get(k) != v for k, v in dict(error='0', hardware_off='1', released='1',
                                              hfrp_held='0', upload_held='0').items()):
        raise RuntimeError('shutdown evidence incomplete; no unload/reload attempted: '+str(state))
    with Device(str(NODE)) as dev:
        info = dev.info()
        if info['retained_jobs'] or info['retained_bytes']:
            raise RuntimeError('DMA accounting not empty after stop')
        try:
            dev.submit(bytes(4096), [(0, 1)], [(0, 1)])
        except OSError as exc:
            if exc.errno != errno.ENODEV:
                raise
        else:
            raise RuntimeError('stopped device accepted a new EXEC')
        # Normal module ownership must prevent removal while a client is open.
        busy = subprocess.run(['rmmod', 'n1x_npu_bootstrap'], capture_output=True, text=True)
        if busy.returncode == 0 or not MODULE.exists():
            raise RuntimeError('module disappeared with an open client')
        record(action='stopped', shutdown=state, info=info,
               exec_after_stop='ENODEV', open_client_prevents_unload=True)
    if int((MODULE/'refcnt').read_text()) != 0:
        raise RuntimeError('module still in use; close remaining clients; no forced unload')
    record(action='unload requested')
    os.sync()
    subprocess.run(['rmmod', 'n1x_npu_bootstrap'], check=True)
    if MODULE.exists() or (ENDPOINT/'driver').exists() or NODE.exists():
        raise RuntimeError('module/device registration remained after unload')
    if (SROOT/'driver').exists():
        raise RuntimeError('SROOT remained bound after unload')
    record(action='unloaded', module_absent=True, endpoints_unbound=True, accel_node_absent=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='new evidence directory')
    parser.add_argument('--cycles', type=int, choices=range(1, 4), default=2)
    args = parser.parse_args()
    if os.geteuid() or platform.release() != KERNEL:
        parser.error('requires root and the audited npu2 kernel')
    profile = Path('/sys/firmware/acpi/platform_profile').read_text().strip()
    module = ROOT/'driver/n1x_npu_bootstrap.ko'
    version = subprocess.check_output(['modinfo', '-F', 'srcversion', str(module)], text=True).strip()
    if MODULE.exists() and ((MODULE/'srcversion').read_text().strip() != version or
                            not (ENDPOINT/'shutdown').exists()):
        parser.error('old pinned module is loaded; reboot the SAME npu2 kernel once before qualification')
    out = args.output_dir.resolve()
    out.mkdir(exist_ok=False)
    owner = int(os.environ['PKEXEC_UID']) if 'PKEXEC_UID' in os.environ else None
    if owner is not None:
        os.chown(out, owner, -1)
    with (out/'events.jsonl').open('x', buffering=1) as log:
        if owner is not None:
            os.fchown(log.fileno(), owner, -1)

        def record(**row):
            data = json.dumps(row)
            print(data, flush=True)
            log.write(data+'\n')
            os.fsync(log.fileno())

        def kernel_log(label):
            result = subprocess.run(['dmesg', '--color=never'], check=True, capture_output=True)
            with (out/(label+'-kernel.log')).open('xb') as file:
                file.write(result.stdout)
                file.flush()
                os.fsync(file.fileno())
                if owner is not None:
                    os.fchown(file.fileno(), owner, -1)

        record(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               kernel=KERNEL, platform_profile=profile, module_srcversion=version,
               module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(), cycles=args.cycles)
        kernel_log('before')

        def start(label):
            record(action='startup requested', label=label)
            subprocess.run([sys.executable, str(ROOT/'scripts/qualify_p16_start.py'),
                            '--through', 'relu_probe', '--enable-accel',
                            '--log', str(out/(label+'-startup.jsonl'))], check=True)
            if (MODULE/'srcversion').read_text().strip() != version:
                raise RuntimeError('loaded source version differs from qualified build')

        def inference(label):
            with Device('/dev/accel/accel0') as dev:
                info = dev.info()
            if not info['capabilities'] & 8:
                raise RuntimeError('controlled-stop capability absent')
            record(action='cuDLA numerical qualification', label=label)
            subprocess.run([sys.executable, str(ROOT/'scripts/qualify_cudla.py'),
                            '--output-dir', str(out/(label+'-cudla'))], check=True)
            record(action='cuDLA changing-input reuse qualification', label=label)
            subprocess.run([sys.executable, str(ROOT/'scripts/qualify_cudla.py'),
                            '--reuse', '--output-dir', str(out/(label+'-reuse'))], check=True)

        try:
            if not MODULE.exists():
                start('initial')
            inference('initial')
            for cycle in range(args.cycles):
                label = f'reload-{cycle+1:02d}'
                stop_and_unload(record)
                start(label)
                inference(label)
                kernel_log(label)
                record(cycle=cycle+1, physical_unload_reload_and_inference_passed=True)
        except BaseException as exc:
            record(failed=True, error=str(exc), automatic_retry=False,
                   module_present=MODULE.exists())
            for name in ('shutdown_status', 'hfrp_status', 'status', 'relu_status'):
                try:
                    record(cached_status=name, value=(ENDPOINT/name).read_text())
                except OSError as status_error:
                    record(cached_status=name, error=str(status_error))
            try:
                kernel_log('failure')
            except (OSError, subprocess.CalledProcessError) as log_error:
                record(kernel_log_error=str(log_error))
            raise
        record(complete=True, physical_reload_cycles=args.cycles,
               numerical_jobs=16*(args.cycles+1), reuse_submissions=5*(args.cycles+1), final_device_running=True)


if __name__ == '__main__':
    main()
