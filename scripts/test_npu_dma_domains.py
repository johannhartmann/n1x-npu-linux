#!/usr/bin/env python3
"""Check group-selection isolation using filesystem fixtures, not real sysfs."""
from pathlib import Path
import tempfile
from select_npu_dma_domains import plan, apply, ENDPOINTS


def fixture(root):
    suppliers = {'NVDA200A:00': 'smmu3.0x0000000012000000.iommu',
                 'NVDA8600:00': 'smmu3.0x0000000005000000.iommu'}
    for index, name in enumerate(ENDPOINTS):
        supplier = suppliers[name]
        dev = root/'bus/platform/devices'/name
        dev.mkdir(parents=True)
        smmu = root/'suppliers'/supplier
        smmu.mkdir(parents=True)
        (dev/'iommu').symlink_to(smmu)
        group = root/'kernel/iommu_groups'/str(index)
        (group/'devices').mkdir(parents=True)
        (group/'devices'/name).symlink_to(dev)
        (group/'type').write_text('identity\n')
        (dev/'iommu_group').symlink_to(group)


def main():
    count = 0
    for case in ('valid', 'already_dma', 'already_fq', 'shared', 'bound', 'supplier', 'domain', 'foreign_group'):
        with tempfile.TemporaryDirectory(prefix='n1x-domains-') as tmp:
            root = Path(tmp)
            fixture(root)
            dev = root/'bus/platform/devices/NVDA8600:00'
            group = (dev/'iommu_group').resolve()
            if case in ('already_dma', 'already_fq'):
                for item in (root/'kernel/iommu_groups').glob('*/type'):
                    item.write_text('DMA\n' if case == 'already_dma' else 'DMA-FQ\n')
            elif case == 'shared':
                (group/'devices/OTHER:00').touch()
            elif case == 'bound':
                (dev/'driver').symlink_to(root/'foreign-driver')
            elif case == 'supplier':
                (dev/'iommu').unlink()
                other = root/'wrong-smmu'
                other.mkdir()
                (dev/'iommu').symlink_to(other)
            elif case == 'domain':
                (group/'type').write_text('unmanaged\n')
            elif case == 'foreign_group':
                (dev/'iommu_group').unlink()
                (dev/'iommu_group').symlink_to(root)
            before = [p.read_bytes() for p in sorted((root/'kernel/iommu_groups').glob('*/type'))]
            try:
                rows = plan(root)
            except ValueError:
                assert case not in ('valid', 'already_dma', 'already_fq')
                assert before == [p.read_bytes() for p in sorted((root/'kernel/iommu_groups').glob('*/type'))]
            else:
                assert case in ('valid', 'already_dma', 'already_fq') and len(rows) == 2
                desired = 'DMA-FQ' if case == 'already_fq' else 'DMA'
                apply(rows)
                assert [p.read_text() for p in sorted((root/'kernel/iommu_groups').glob('*/type'))] == [desired+'\n']*2
                # An already selected domain is preserved and can be rechecked.
                assert all(r['current'] == r['target'] == desired for r in plan(root))
            count += 1
    print(f'{count} per-device DMA-domain fixtures passed; no real sysfs access')


if __name__ == '__main__':
    main()
