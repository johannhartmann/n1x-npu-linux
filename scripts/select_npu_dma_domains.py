#!/usr/bin/env python3
"""Select translated DMA domains for the audited, unbound NPU/SROOT groups.

Default is inspection; --apply writes only those groups' type controls. This
changes SMMU configuration, not NPU registers. Never changes global policy.
"""
import argparse
import json
import os
from pathlib import Path
import platform

EXPECTED = {'NVDA200A:00': 'smmu3.0x0000000035000000',
            'NVDA8600:00': 'smmu3.0x0000000014900000'}


def plan(sysroot=Path('/sys')):
    for field, value in {'sys_vendor': 'ASUS', 'product_name': 'ProArt P16 H7607BA',
                         'bios_version': 'H7607BA.300'}.items():
        if (sysroot/'class/dmi/id'/field).read_text().strip() != value:
            raise ValueError('machine/BIOS differs from the audited P16')
    groups = (sysroot/'kernel/iommu_groups').resolve(strict=True)
    rows = []
    for name, supplier in EXPECTED.items():
        dev = sysroot/'bus/platform/devices'/name
        if (dev/'driver').exists() or (dev/'driver').is_symlink():
            raise ValueError(name+' is bound; unload its driver before selecting a domain')
        if (dev/'iommu').resolve(strict=True).name != supplier:
            raise ValueError(name+' has an unexpected SMMU supplier')
        group = (dev/'iommu_group').resolve(strict=True)
        if group.parent != groups or not group.name.isdecimal():
            raise ValueError(name+' has an unexpected IOMMU group path')
        if {p.name for p in (group/'devices').iterdir()} != {name}:
            raise ValueError(name+' shares its group with another device')
        current = (group/'type').read_text().strip()
        if current not in ('identity', 'DMA', 'DMA-FQ'):
            raise ValueError(name+' has an unsupported domain type '+current)
        rows.append({'device': name, 'group': str(group), 'current': current,
                     'target': 'DMA' if current == 'identity' else current})
    return rows


def apply(rows):
    # The kernel serializes the change and rejects groups whose drivers bind
    # after inspection. Never unbind devices or force a failed domain switch.
    for row in rows:
        path = Path(row['group'])/'type'
        if row['current'] == 'identity':
            path.write_text('DMA\n')
        actual = path.read_text().strip()
        if actual != row['target']:
            raise RuntimeError(row['device']+' domain did not reach the requested type')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if platform.release() not in ('7.2.8-23-omarchy-p16-asus-npu1',
                                  '7.2.8-23-omarchy-p16-asus-npu2'):
        parser.error('requires the isolated SROOT test kernel; current kernel is unchanged')
    rows = plan()
    print(json.dumps(rows, indent=2), flush=True)
    if args.apply:
        if os.geteuid() != 0:
            parser.error('--apply requires root')
        apply(rows)
        print('Selected per-device DMA domains; global IOMMU policy unchanged')


if __name__ == '__main__':
    main()
