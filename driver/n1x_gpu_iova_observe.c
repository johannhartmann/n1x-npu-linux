// SPDX-License-Identifier: GPL-2.0-only
/* Inspect known candidate IOVAs without mapping or reading their contents.
 * No PCI driver registration, runtime-PM call, DMA allocation, MMIO or RPC.
 * On the audited arm-smmu-v3 driver iommu_iova_to_phys walks CPU page tables.
 * A zero result means no translation in this domain, not absent firmware.
 */
#include <linux/device.h>
#include <linux/dmi.h>
#include <linux/iommu.h>
#include <linux/module.h>
#include <linux/pci.h>
#include <linux/string.h>
#include <linux/utsname.h>

static unsigned long cpu_pages[10];
module_param_array(cpu_pages, ulong, NULL, 0400);
MODULE_PARM_DESC(cpu_pages, "Read-only sampled translations; all entries overwritten at module init");

static int __init n1x_gpu_iova_init(void)
{
	struct pci_dev *pdev;
	struct iommu_domain *domain;
	phys_addr_t first[10], second[10];
	unsigned int i;
	int ret = 0;
	/* First nine: scratch candidate (36 KiB). Last: cached FRTS offset,
	 * queried only as an IOVA hypothesis; it is NOT a known CPU address.
	 */
	const dma_addr_t addresses[] = {
		0xfc000000, 0xfc001000, 0xfc002000, 0xfc003000, 0xfc004000,
		0xfc005000, 0xfc006000, 0xfc007000, 0xfc008000, 0xb9500000,
	};

	if (!dmi_match(DMI_SYS_VENDOR, "ASUS") ||
	    !dmi_match(DMI_PRODUCT_NAME, "ProArt P16 H7607BA") ||
	    !dmi_match(DMI_BIOS_VERSION, "H7607BA.300") ||
	    strcmp(utsname()->release, "7.2.8-23-omarchy-p16-asus-npu2"))
		return -ENODEV;
	pdev = pci_get_domain_bus_and_slot(0x000f, 1, PCI_DEVFN(0, 0));
	if (!pdev)
		return -ENODEV;
	device_lock(&pdev->dev);
	if (pdev->vendor != PCI_VENDOR_ID_NVIDIA || pdev->device != 0x2e03 ||
	    !pdev->dev.driver || strcmp(pdev->dev.driver->name, "nvidia")) {
		ret = -ENODEV;
		goto out;
	}
	domain = iommu_get_domain_for_dev(&pdev->dev);
	if (!domain || domain->type != IOMMU_DOMAIN_DMA) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	/* Keep the translation implementation explicit; never dispatch to an
	 * unaudited driver's possible hardware translation request.
	 */
	if (!domain->ops || !domain->ops->iova_to_phys || !pdev->dev.iommu ||
	    !pdev->dev.iommu->iommu_dev || !pdev->dev.iommu->iommu_dev->dev ||
	    !pdev->dev.iommu->iommu_dev->dev->parent ||
	    strcmp(dev_driver_string(pdev->dev.iommu->iommu_dev->dev->parent),
		   "arm-smmu-v3")) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	for (i = 0; i < ARRAY_SIZE(addresses); i++)
		first[i] = iommu_iova_to_phys(domain, addresses[i]);
	for (i = 0; i < ARRAY_SIZE(addresses); i++) {
		second[i] = iommu_iova_to_phys(domain, addresses[i]);
		if (first[i] != second[i]) {
			ret = -EAGAIN;
			goto out;
		}
	}
	pr_info("n1x-gpu-iova: PCI=%s domain=DMA stable_samples=2 (no target-memory reads)\n",
		pci_name(pdev));
	for (i = 0; i < ARRAY_SIZE(addresses); i++)
		cpu_pages[i] = first[i];
	for (i = 0; i < ARRAY_SIZE(addresses); i++)
		pr_info("n1x-gpu-iova: candidate=%s iova=0x%llx cpu_pa=%pa mapped=%u\n",
			i == 9 ? "frts-offset-hypothesis" : "scratch",
			(unsigned long long)addresses[i], &first[i], !!first[i]);
out:
	device_unlock(&pdev->dev);
	pci_dev_put(pdev);
	return ret;
}

static void __exit n1x_gpu_iova_exit(void) { }
module_init(n1x_gpu_iova_init);
module_exit(n1x_gpu_iova_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Explicit read-only P16 GPU IOMMU candidate translation observer");
