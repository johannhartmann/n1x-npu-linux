"""Exercise the real SMMUv3 callback with hardware-ID and unrelated-device controls."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = '''static int arm_smmu_def_domain_type(struct device *dev)
{
\tif (dev_is_pci(dev)) {
\t\tstruct pci_dev *pdev = to_pci_dev(dev);

\t\tif (IS_HISI_PTT_DEVICE(pdev))
\t\t\treturn IOMMU_DOMAIN_IDENTITY;

\t\tif (pdev->vendor == PCI_VENDOR_ID_NVIDIA &&
\t\t    pdev->device >= 0x2E00 && pdev->device <= 0x2E3F)
\t\t\treturn IOMMU_DOMAIN_DMA;
\t}

\treturn 0;
}
'''


class TranslatedDMATests(unittest.TestCase):
    def test_n1x_ip_overrides_passthrough_without_changing_unrelated_devices(self):
        with tempfile.TemporaryDirectory(prefix='n1x-dma-policy-') as directory:
            root = Path(directory)
            source = root/'drivers/iommu/arm/arm-smmu-v3/arm-smmu-v3.c'
            source.parent.mkdir(parents=True)
            prefix = '\n'.join(line[1:] for line in (ROOT/'patches/arm-smmu-v3-n1x-translated-dma.patch').read_text().splitlines()[3:6]) + '\n'
            source.write_text(prefix + BASE)
            subprocess.run(['patch', '-p1', '--fuzz=0', '--batch', '-i',
                            str(ROOT/'patches/arm-smmu-v3-n1x-translated-dma.patch')],
                           cwd=root, check=True, capture_output=True)
            code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
struct acpi_device_id {char id[16];unsigned long data;};
struct pci_dev {unsigned vendor,device;};
struct device {const char *hid;bool pci,acpi;struct pci_dev pdev;};
#define IOMMU_DOMAIN_DMA 1
#define IOMMU_DOMAIN_IDENTITY 2
#define PCI_VENDOR_ID_NVIDIA 0x10de
#define PCI_VENDOR_ID_HUAWEI 0x19e5
static bool dev_is_pci(struct device *d){return d->pci;}
static struct pci_dev *to_pci_dev(struct device *d){return &d->pdev;}
static const struct acpi_device_id *acpi_match_device(const struct acpi_device_id *ids,struct device *d)
{if(!d->acpi || !d->hid)return NULL;for(;ids->id[0];ids++)if(!strcmp(ids->id,d->hid))return ids;return NULL;}
''' + source.read_text() + r'''
int main(void)
{
 const char *ids[]={"NVDA200A","NVDA8600","NVDA8301","NVDA8302","NVDA8303"};
 struct device d={.acpi=true};
 for(unsigned i=0;i<5;i++){d.hid=ids[i];assert(arm_smmu_def_domain_type(&d)==IOMMU_DOMAIN_DMA);}
 d.acpi=false;assert(!arm_smmu_def_domain_type(&d));
 d.acpi=true;d.hid="NVDA8304";assert(!arm_smmu_def_domain_type(&d));
 d.hid="NVDA1000";assert(!arm_smmu_def_domain_type(&d));
 d.hid=NULL;assert(!arm_smmu_def_domain_type(&d));
 d.pci=true;d.pdev=(struct pci_dev){0x19e5,0xa12e};
 assert(arm_smmu_def_domain_type(&d)==IOMMU_DOMAIN_IDENTITY);
 d.pdev=(struct pci_dev){0x10de,0x2e00};assert(arm_smmu_def_domain_type(&d)==IOMMU_DOMAIN_DMA);
 d.pdev.device=0x2e3f;assert(arm_smmu_def_domain_type(&d)==IOMMU_DOMAIN_DMA);
 d.pdev.device=0x2e40;assert(!arm_smmu_def_domain_type(&d));
 d.pdev=(struct pci_dev){0x1234,0x2e00};assert(!arm_smmu_def_domain_type(&d));
}
'''
            (root/'test.c').write_text(code)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', str(root/'test.c'),
                            '-o', str(root/'test')], check=True)
            subprocess.run([str(root/'test')], check=True)


if __name__ == "__main__":
    unittest.main()
