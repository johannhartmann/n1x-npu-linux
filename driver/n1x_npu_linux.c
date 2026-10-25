// SPDX-License-Identifier: GPL-2.0-only
/* Initial Linux platform backend. No NPU register writes, firmware requests,
 * IRQ handlers or power/reset operations. DMA allocation testing is opt-in.
 * No MODULE_DEVICE_TABLE: this experimental driver is never auto-loaded.
 */
#include <linux/acpi.h>
#include <linux/dma-map-ops.h>
#include <linux/dma-mapping.h>
#include <linux/dmi.h>
#include <linux/io.h>
#include <linux/iommu.h>
#include <linux/iommu-dma.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include "n1x_dla_registers.h"

static bool read_mmio;
module_param(read_mmio, bool, 0400);
MODULE_PARM_DESC(read_mmio, "Allow explicit read-only register snapshots (default false)");

static bool test_dma;
module_param(test_dma, bool, 0400);
MODULE_PARM_DESC(test_dma, "Allocate/check/free an unpublished DMA buffer at probe (default false)");

struct n1x_linux {
	void __iomem *base;
	resource_size_t bytes;
	u32 first_offset;
	bool sroot;
};

/* Allocation-only test: no device receives this address, so freeing on any
 * result is safe. This rule must NOT be reused after an upload/task submission.
 */
static int n1x_test_dma_allocation(struct device *dev, bool sroot)
{
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct iommu_domain *domain = iommu_get_domain_for_dev(dev);
	u64 minimum = sroot ? 0xe0000000ULL : 1;
	u64 maximum = DMA_BIT_MASK(sroot ? 32 : 36);
	size_t bytes = sroot ? 67508 : 4096;
	dma_addr_t address;
	void *buffer;
	int ret;

	if (!adev || !adev->flags.cca_seen || !dev->dma_mask)
		return -EOPNOTSUPP;
	if (adev->flags.coherent_dma != !sroot || dev_is_dma_coherent(dev) != !sroot)
		return -EINVAL;
	if (!domain || !iommu_is_dma_domain(domain) || !use_dma_iommu(dev))
		return -EOPNOTSUPP;
	ret = dma_set_mask_and_coherent(dev, maximum);
	if (ret)
		return ret;
	buffer = dma_alloc_coherent(dev, bytes, &address, GFP_KERNEL);
	if (!buffer)
		return -ENOMEM;
	if (address < minimum || address > maximum || bytes - 1 > maximum - address ||
	    (address & 255)) {
		ret = -ERANGE;
		goto free;
	}
	memset(buffer, 0xa5, bytes);
	if (memchr_inv(buffer, 0xa5, bytes)) {
		ret = -EIO;
		goto free;
	}
	dev_info(dev, "DMA allocation-only test: %zu bytes at %pad; no device transfer\n",
		 bytes, &address);
	ret = 0;
free:
	dma_free_coherent(dev, bytes, buffer, address);
	return ret;
}

static int n1x_linux_read32(struct n1x_linux *npu, u32 logical, u32 *value)
{
	u32 offset;
	int ret;

	if (npu->sroot) {
		if ((logical & 3) || logical < npu->first_offset)
			return -ERANGE;
		offset = logical - npu->first_offset;
		if (npu->bytes < 4 || offset > npu->bytes - 4)
			return -ERANGE;
	} else {
		/* Snapshot registers have identical offsets in Windows maps 1/2.
		 * This is not a claim that live remapper selection is established.
		 */
		ret = n1x_dla_register_offset(N1X_DLA_MAP_WINDOWS_1, logical,
					      npu->bytes, &offset);
		if (ret)
			return ret;
	}
	*value = readl(npu->base + offset);
	return 0;
}

static ssize_t dma_state_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	struct iommu_fwspec *spec = dev_iommu_fwspec_get(dev);
	struct iommu_domain *domain = iommu_get_domain_for_dev(dev);
	ssize_t size;
	unsigned int i;

	size = sysfs_emit(buf, "acpi_cca_seen=%u acpi_coherent=%u dma_coherent=%u iommu_domain=%d\n",
			  ACPI_COMPANION(dev)->flags.cca_seen,
			  ACPI_COMPANION(dev)->flags.coherent_dma,
			  dev_is_dma_coherent(dev), domain ? (int)domain->type : -1);
	if (spec)
		for (i = 0; i < spec->num_ids && size < PAGE_SIZE - 32; i++)
			size += sysfs_emit_at(buf, size, "stream_id[%u]=0x%x\n", i, spec->ids[i]);
	return size;
}
static DEVICE_ATTR_RO(dma_state);

static ssize_t registers_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	/* Status-only registers read by the original Windows accessors.
	 * Never read SROOT RX/TX data FIFOs (4/7), which consume entries.
	 */
	static const u32 dla[] = {0x4c, 0x8, 0x40, 0x44, 0xd2c};
	static const u32 sroot[] = {2 * 16, 5 * 16, 8 * 16, 11 * 16};
	struct n1x_linux *npu = dev_get_drvdata(dev);
	const u32 *offsets = npu->sroot ? sroot : dla;
	size_t count = npu->sroot ? ARRAY_SIZE(sroot) : ARRAY_SIZE(dla);
	ssize_t size = 0;
	size_t i;
	u32 value;
	int ret;

	if (!read_mmio)
		return -EACCES;
	for (i = 0; i < count; i++) {
		ret = n1x_linux_read32(npu, offsets[i], &value);
		if (ret)
			return ret;
		size += sysfs_emit_at(buf, size, "0x%04x=0x%08x\n", offsets[i], value);
	}
	return size;
}
static DEVICE_ATTR(registers, 0400, registers_show, NULL);

static struct attribute *n1x_attrs[] = {
	&dev_attr_dma_state.attr,
	&dev_attr_registers.attr,
	NULL,
};
ATTRIBUTE_GROUPS(n1x);

static int n1x_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct n1x_linux *npu;
	struct resource *first, *second;
	struct resource registers = { .flags = IORESOURCE_MEM };
	unsigned long long revision;
	bool sroot;

	if (!dmi_match(DMI_SYS_VENDOR, "ASUS") ||
	    !dmi_match(DMI_PRODUCT_NAME, "ProArt P16 H7607BA") ||
	    !dmi_match(DMI_BIOS_VERSION, "H7607BA.300") || !adev)
		return -ENODEV;
	sroot = !strcmp(acpi_device_hid(adev), "NVDA8600");
	if (ACPI_FAILURE(acpi_evaluate_integer(adev->handle, "_HRV", NULL, &revision)) ||
	    revision != (sroot ? 1 : 0x34))
		return dev_err_probe(dev, -ENODEV, "unsupported hardware revision\n");
	first = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	second = platform_get_resource(pdev, IORESOURCE_MEM, 1);
	if (!first || !second || platform_get_resource(pdev, IORESOURCE_MEM, 2))
		return -EINVAL;
	if (first->start != (sroot ? 0x18010000 : 0x19000000) ||
	    resource_size(first) != (sroot ? 0x1000 : 0x40000) ||
	    second->start != (sroot ? 0x18010000 : 0x31380000) ||
	    resource_size(second) != (sroot ? 0x10 : 0x10000))
		return dev_err_probe(dev, -ENODEV, "resources differ from audited P16 tables\n");
	npu = devm_kzalloc(dev, sizeof(*npu), GFP_KERNEL);
	if (!npu)
		return -ENOMEM;
	npu->sroot = sroot;
	registers.start = first->start;
	registers.end = first->end;
	registers.name = dev_name(dev);
	if (sroot) {
		/* The stride descriptor occupies indices 0's first 16 bytes
		 * in Linux's resource tree. Request only the actual mailbox
		 * registers (indices 1..12), without bypassing its reservation
		 * or claiming a parent region across a child descriptor.
		 */
		npu->first_offset = resource_size(second);
		registers.start += npu->first_offset;
		registers.end = first->start + 12 * npu->first_offset + 3;
	}
	npu->bytes = resource_size(&registers);
	npu->base = devm_ioremap_resource(dev, &registers);
	if (IS_ERR(npu->base))
		return PTR_ERR(npu->base);
	if (test_dma) {
		int ret = n1x_test_dma_allocation(dev, sroot);

		if (ret)
			return dev_err_probe(dev, ret, "DMA allocation test failed; check CCA/domain/window\n");
	}
	platform_set_drvdata(pdev, npu);
	dev_info(dev, "observation backend attached; MMIO reads %s; no write path\n",
		 read_mmio ? "explicitly enabled" : "disabled");
	return 0;
}

static const struct acpi_device_id n1x_acpi_ids[] = {
	{"NVDA200A", 0},
	{"NVDA8600", 0},
	{},
};

static struct platform_driver n1x_driver = {
	.probe = n1x_probe,
	.driver = {
		.name = "n1x-npu-observe",
		.acpi_match_table = n1x_acpi_ids,
		.dev_groups = n1x_groups,
	},
};
module_platform_driver(n1x_driver);
MODULE_DESCRIPTION("N1X NPU platform observation backend; no firmware boot/inference");
MODULE_LICENSE("GPL");
