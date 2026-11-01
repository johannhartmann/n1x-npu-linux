#!/usr/bin/env python3
"""Portable fail-closed checks for the boot-time NPU starter."""
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import npu_autostart as target


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def fixture(root):
    sysroot, firmware, devroot = root/'sys', root/'firmware', root/'dev'
    for name, value in target.EXPECTED_DMI.items():
        write(sysroot/'class/dmi/id'/name, value+'\n')
    for name, (size, _) in target.FIRMWARE.items():
        data = bytes(size)
        path = firmware/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        target.FIRMWARE[name] = (size, hashlib.sha256(data).hexdigest())
    groups = sysroot/'kernel/iommu_groups'
    groups.mkdir(parents=True)
    for number, (name, supplier) in enumerate(target.EXPECTED_IOMMU.items(), 11):
        group = groups/str(number)
        (group/'devices').mkdir(parents=True)
        write(group/'type', 'identity\n')
        dev = sysroot/'bus/platform/devices'/name
        dev.mkdir(parents=True)
        (dev/'iommu').symlink_to(sysroot/'devices'/supplier)
        (sysroot/'devices'/supplier).mkdir(parents=True)
        (dev/'iommu_group').symlink_to(group)
        (group/'devices'/name).symlink_to(dev)
    return sysroot, firmware, devroot


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-autostart-') as name:
        root = Path(name)
        original_firmware = target.FIRMWARE.copy()
        try:
            sysroot, firmware, devroot = fixture(root)
            target.validate_host(sysroot, firmware, target.KERNEL)
            rows = target.dma_plan(sysroot)
            assert len(rows) == 2 and all(row[2:] == ('identity', 'DMA') for row in rows)
            target.select_dma(rows)
            assert all(row[1].read_text() == 'DMA\n' for row in rows)
            write(sysroot/'class/dmi/id/bios_version', 'different\n')
            try:
                target.validate_host(sysroot, firmware, target.KERNEL)
            except RuntimeError:
                pass
            else:
                raise AssertionError('different BIOS accepted')
        finally:
            target.FIRMWARE.clear()
            target.FIRMWARE.update(original_firmware)

    # Every trigger is one write; an error is surfaced and never retried.
    calls = []
    with patch('npu_autostart.os.open', return_value=7), \
            patch('npu_autostart.os.write', side_effect=lambda fd, data: calls.append(data) or len(data)), \
            patch('npu_autostart.os.close'):
        target.trigger(Path('/modeled/sysfs'), b'run\n')
    assert calls == [b'run\n']
    print('NPU autostart host, firmware, IOMMU and no-retry gates passed')


if __name__ == '__main__':
    main()
