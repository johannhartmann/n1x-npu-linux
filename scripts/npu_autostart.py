#!/usr/bin/env python3
"""Start the qualified RTX Spark NPU once, fail closed and never retry.

This is the non-interactive counterpart of ``qualify_p16_start.py``.  It is
intended for the systemd unit shipped with the module package and deliberately
accepts only the exact machine, BIOS, kernel, firmware and isolated IOMMU
topology that were physically qualified.
"""
import hashlib
import os
from pathlib import Path
import platform
import subprocess

KERNEL = '7.2.8-23-rtx-spark-npu'
MODULE = 'n1x_npu_bootstrap'
ENDPOINT = 'NVDA200A:00'
ENDPOINTS = (ENDPOINT, 'NVDA8600:00')
EXPECTED_DMI = {
    'sys_vendor': 'ASUS',
    'product_name': 'ProArt P16 H7607BA',
    'bios_version': 'H7607BA.300',
}
EXPECTED_IOMMU = {
    ENDPOINT: 'smmu3.0x0000000035000000',
    'NVDA8600:00': 'smmu3.0x0000000014900000',
}
FIRMWARE = {
    'p16-sroot-v0.bin': (
        67508,
        '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4',
    ),
}
MODULE_OPTIONS = (
    'firmware_variant=0', 'enable_hfrp=1', 'enable_upload=1', 'enable_ping=1',
    'enable_region5=1', 'enable_config_snapshot=1', 'enable_config_apply=1',
    'enable_lpwr_config=1', 'enable_bypass_task=1', 'enable_accel=1',
)
PHASES = (
    ('prepare_hfrp', b'prepare\n', 'hfrp_status', 'power_accepted'),
    ('upload', b'upload\n', 'status', 'idle_observed'),
    ('ping', b'ping\n', 'status', 'ping_verified'),
    ('init_region5', b'init\n', 'status', 'region5_registered'),
    ('read_config', b'read\n', 'status', 'config_valid'),
    ('apply_config', b'apply\n', 'status', 'config_apply_accepted'),
    ('apply_lpwr', b'apply\n', 'status', 'lpwr_accepted'),
    ('bypass_task', b'run\n', 'status', 'task_verified'),
)
FINAL_STATUS = (
    'dla_bound', 'sroot_bound', 'idle_observed', 'ping_verified',
    'region5_registered', 'config_valid', 'config_apply_accepted',
    'lpwr_accepted', 'task_verified',
)


def fields(text):
    return dict(item.split('=', 1) for item in text.split() if '=' in item)


def verify_file(path, expected_size, expected_digest):
    data = path.read_bytes()
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_digest:
        raise RuntimeError(f'unqualified firmware file: {path}')


def validate_host(sysroot=Path('/sys'), firmware_root=Path('/usr/lib/firmware/nvidia/n1x'),
                  release=None, strict=False):
    release = platform.release() if release is None else release
    if strict:
        if release != KERNEL:
            raise RuntimeError(f'requires {KERNEL}; running {release}')
        for name, expected in EXPECTED_DMI.items():
            actual = (sysroot/'class/dmi/id'/name).read_text().strip()
            if actual != expected:
                raise RuntimeError(f'unqualified host {name}: {actual!r}')
    for name in ENDPOINTS:
        if not (sysroot/'bus/platform/devices'/name).exists():
            raise RuntimeError(f'missing ACPI NPU endpoint: {name}')
    for name, (size, digest) in FIRMWARE.items():
        verify_file(firmware_root/name, size, digest)


def dma_plan(sysroot=Path('/sys'), strict=False):
    groups = (sysroot/'kernel/iommu_groups').resolve(strict=True)
    rows = []
    for name in ENDPOINTS:
        dev = sysroot/'bus/platform/devices'/name
        if (dev/'driver').exists() or (dev/'driver').is_symlink():
            raise RuntimeError(f'{name} is already bound before startup')
        supplier = (dev/'iommu').resolve(strict=True).name
        if strict and supplier != EXPECTED_IOMMU[name]:
            raise RuntimeError(f'{name} has an unexpected SMMU supplier')
        if not supplier.startswith('smmu3.'):
            raise RuntimeError(f'{name} is not routed through an ARM SMMUv3 ({supplier})')
        group = (dev/'iommu_group').resolve(strict=True)
        if group.parent != groups or not group.name.isdecimal():
            raise RuntimeError(f'{name} has an unexpected IOMMU group')
        if {item.name for item in (group/'devices').iterdir()} != {name}:
            raise RuntimeError(f'{name} does not have an isolated IOMMU group')
        current = (group/'type').read_text().strip()
        if current not in ('identity', 'DMA', 'DMA-FQ'):
            raise RuntimeError(f'{name} has unsupported IOMMU mode {current}')
        rows.append((name, group/'type', current, 'DMA' if current == 'identity' else current))
    return rows


def select_dma(rows):
    for name, control, current, target in rows:
        if current == 'identity':
            control.write_text('DMA\n')
        if control.read_text().strip() != target:
            raise RuntimeError(f'{name} did not enter its qualified IOMMU mode')


def trigger(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError('short sysfs trigger; operation was not retried')
    finally:
        os.close(descriptor)


def verify_ready(sysroot=Path('/sys'), devroot=Path('/dev')):
    endpoint = sysroot/'bus/platform/devices'/ENDPOINT
    if (endpoint/'driver').resolve(strict=True).name != 'n1x-npu-bootstrap':
        raise RuntimeError('NPU endpoint is not bound to the qualified driver')
    hfrp = fields((endpoint/'hfrp_status').read_text())
    status = fields((endpoint/'status').read_text())
    if any(hfrp.get(key) != value for key, value in
           {'power_accepted': '1', 'configured': '1', 'phase': '5', 'error': '0'}.items()):
        raise RuntimeError('NPU HFRP startup evidence is incomplete')
    if any(status.get(key) != '1' for key in FINAL_STATUS) or status.get('queue_poisoned', '0') != '0':
        raise RuntimeError('NPU firmware startup evidence is incomplete')
    nodes = [node for node in (sysroot/'class/accel').glob('accel*')
             if (node/'device').resolve() == endpoint.resolve()]
    if len(nodes) != 1 or not (devroot/'accel'/nodes[0].name).exists():
        raise RuntimeError('qualified NPU accel node is missing')
    return nodes[0].name


def start(sysroot=Path('/sys'), firmware_root=Path('/usr/lib/firmware/nvidia/n1x'),
          devroot=Path('/dev'), release=None, run=subprocess.run):
    if os.geteuid():
        raise RuntimeError('NPU startup requires root')
    validate_host(sysroot, firmware_root, release)
    loaded = sysroot/'module'/MODULE
    if loaded.exists():
        node = verify_ready(sysroot, devroot)
        print(f'RTX Spark NPU was already ready at /dev/accel/{node}')
        return
    rows = dma_plan(sysroot)
    select_dma(rows)
    run(['/usr/bin/modprobe', MODULE, *MODULE_OPTIONS], check=True)
    endpoint = sysroot/'bus/platform/devices'/ENDPOINT
    for phase, payload, status_file, success in PHASES:
        os.sync()
        trigger(endpoint/phase, payload)
        if fields((endpoint/status_file).read_text()).get(success) != '1':
            raise RuntimeError(f'{phase} returned without {success}; no retry or unload attempted')
        print(f'RTX Spark NPU startup: {phase} verified', flush=True)
    node = verify_ready(sysroot, devroot)
    print(f'RTX Spark NPU is ready at /dev/accel/{node}')


if __name__ == '__main__':
    start()
