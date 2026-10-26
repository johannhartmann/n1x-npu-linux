// SPDX-License-Identifier: GPL-2.0-only
/* One opt-in early RAM snapshot for P16 BIOS 300, not an NPU boot driver. */
#include <linux/efi.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/kobject.h>
#include <linux/memblock.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/unaligned.h>
#include <asm/early_ioremap.h>

#define FRTS_CANDIDATE 0xfc000000ULL
#define FRTS_BYTES 0x9000

static bool requested __initdata;
static bool captured __ro_after_init;
static u8 snapshot[FRTS_BYTES] __ro_after_init;

void __init n1x_frts_snapshot_early(void);

static int __init n1x_frts_snapshot_param(char *value)
{
	/* Fixed board/candidate acknowledgement, deliberately no address parser. */
	if (!value || strcmp(value, "p16-bios300"))
		return -EINVAL;
	requested = true;
	return 0;
}
early_param("n1x_frts_snapshot", n1x_frts_snapshot_param);

void __init n1x_frts_snapshot_early(void)
{
	struct memblock_region *region;
	u8 header[40];
	const void *source;
	u32 offset, size;
	bool ordinary_ram = false;

	if (!requested)
		return;
	/* Called after EFI and kernel/initrd reservations, before paging/SWIOTLB.
	 * Accept only one ordinary RAM region, with no pre-existing reservation.
	 * No PCI/ACPI binding, MMIO, register writes, source writes or DMA.
	 */
	if (!efi_enabled(EFI_BOOT) ||
	    memblock_is_region_reserved(FRTS_CANDIDATE, FRTS_BYTES))
		goto rejected;
	for_each_mem_region(region) {
		if (region->base <= FRTS_CANDIDATE &&
		    FRTS_CANDIDATE + FRTS_BYTES <= region->base + region->size &&
		    region->flags == MEMBLOCK_NONE) {
			ordinary_ram = true;
			break;
		}
	}
	if (!ordinary_ram)
		goto rejected;
	source = early_memremap_ro(FRTS_CANDIDATE, FRTS_BYTES);
	if (!source) {
		pr_info("n1x-frts: candidate RAM could not be mapped\n");
		return;
	}
	memcpy(header, source, sizeof(header));
	offset = get_unaligned_le32(header + 32);
	size = get_unaligned_le32(header + 36);
	if (memcmp(header, "GFWI", 4) || offset < sizeof(header) ||
	    offset >= FRTS_BYTES || !size || size > FRTS_BYTES - offset) {
		pr_info("n1x-frts: candidate has no bounded GFWI ROM member\n");
		goto unmap;
	}
	memcpy(snapshot, source, sizeof(snapshot));
	if (memcmp(snapshot, header, sizeof(header)) ||
	    memcmp(snapshot, source, sizeof(snapshot))) {
		memzero_explicit(snapshot, sizeof(snapshot));
		pr_info("n1x-frts: candidate changed during capture\n");
		goto unmap;
	}
	captured = true;
	pr_info("n1x-frts: captured %u bytes before SWIOTLB; ROM offset %#x size %#x\n",
		FRTS_BYTES, offset, size);
unmap:
	early_memunmap((void *)source, FRTS_BYTES);
	return;
rejected:
	pr_info("n1x-frts: candidate is not unreserved ordinary EFI RAM\n");
}

static const struct bin_attribute frts_attr = {
	.attr = { .name = "n1x_frts_snapshot", .mode = 0400 },
	.size = FRTS_BYTES,
	.read = sysfs_bin_attr_simple_read,
	.private = snapshot,
};

static int __init n1x_frts_snapshot_publish(void)
{
	if (!captured)
		return 0;
	return sysfs_create_bin_file(firmware_kobj, &frts_attr);
}
late_initcall(n1x_frts_snapshot_publish);
