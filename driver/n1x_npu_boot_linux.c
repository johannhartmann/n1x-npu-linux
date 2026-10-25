// SPDX-License-Identifier: GPL-2.0-only
/* Explicit-load, one-attempt SROOT upload qualification backend.
 * No aliases, inference UAPI, automatic upload, IRQ setup or reset/recovery.
 * After an attempted upload, retain DMA/runtime/module references until reboot.
 */
#include <crypto/sha2.h>
#include <linux/acpi.h>
#include <linux/capability.h>
#include <linux/delay.h>
#include <linux/dma-map-ops.h>
#include <linux/dma-mapping.h>
#include <linux/dmi.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/iommu.h>
#include <linux/iommu-dma.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/utsname.h>
#include "n1x_dla_boot.h"
#include "n1x_dla_command.h"
#include "n1x_dla_registers.h"

#define N1X_BOOT_KERNEL "7.2.8-23-omarchy-p16-asus-npu1"
#define N1X_BOOT_FIRMWARE "nvidia/n1x/p16-sroot-v0.bin"
#define N1X_BOOT_BYTES 67508

static bool enable_upload;
module_param(enable_upload, bool, 0400);
MODULE_PARM_DESC(enable_upload, "Permit root's explicit one-attempt upload trigger (default false)");

static bool enable_ping;
module_param(enable_ping, bool, 0400);
MODULE_PARM_DESC(enable_ping, "Permit one explicit DMA-word ping after successful upload phase (default false)");

struct n1x_boot_endpoint {
	struct device *dev;
	void __iomem *base;
	resource_size_t bytes;
	u32 first;
	bool sroot;
};

static DEFINE_MUTEX(boot_lock);
static struct n1x_boot_endpoint *boot_dla, *boot_sroot;
static struct n1x_dla_sroot_load_result boot_result;
static void *boot_buffer;
static dma_addr_t boot_dma;
static int boot_error;
static bool boot_attempted;
static struct {
	void *buffer;
	dma_addr_t dma;
	u32 owned, mask_before, masked, word;
	bool attempted, verified;
	int error;
} boot_ping;

static bool n1x_boot_machine(void)
{
	return !strcmp(init_uts_ns.name.release, N1X_BOOT_KERNEL) &&
		dmi_match(DMI_SYS_VENDOR, "ASUS") &&
		dmi_match(DMI_PRODUCT_NAME, "ProArt P16 H7607BA") &&
		dmi_match(DMI_BIOS_VERSION, "H7607BA.300");
}

static int n1x_boot_group_member(struct device *member, void *data)
{
	return member == data ? 0 : -EINVAL;
}

static int n1x_boot_dma_gate(struct n1x_boot_endpoint *endpoint)
{
	struct device *dev = endpoint->dev;
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct iommu_domain *domain = iommu_get_domain_for_dev(dev);
	struct iommu_fwspec *spec = dev_iommu_fwspec_get(dev);
	struct iommu_group *group;
	int err;

	if (!adev || !adev->flags.cca_seen || !dev->dma_mask ||
	    adev->flags.coherent_dma != !endpoint->sroot ||
	    dev_is_dma_coherent(dev) != !endpoint->sroot ||
	    !domain || !iommu_is_dma_domain(domain) || !use_dma_iommu(dev) ||
	    !spec || spec->num_ids != 1 || spec->ids[0] != (endpoint->sroot ? 1 : 0x2b))
		return -EOPNOTSUPP;
	group = iommu_group_get(dev);
	if (!group)
		return -EOPNOTSUPP;
	err = iommu_group_for_each_dev(group, dev, n1x_boot_group_member);
	iommu_group_put(group);
	return err;
}

static int n1x_boot_offset(struct n1x_boot_endpoint *endpoint, u32 logical,
			   bool write, u32 *offset)
{
	if (endpoint->sroot) {
		u32 index = logical / 16;

		if (logical % 16 || (write ?
		    (index != 1 && index != 3 && index != 6 && index != 9 && index != 12) :
		    (index != 2 && index != 4 && index != 5 && index != 7 && index != 8 && index != 11)))
			return -EINVAL;
		if (logical < endpoint->first)
			return -ERANGE;
		*offset = logical - endpoint->first;
		if (endpoint->bytes < 4 || *offset > endpoint->bytes - 4)
			return -ERANGE;
		return 0;
	}
	/* Upload has no DLA writer. The separate opt-in ping uses only the
	 * recovered route/mask/method registers; no core-start/reset/IRQ ACK.
	 */
	if (write ? (logical != 0xd24 && logical != 0x6c && logical != 0x68) :
	    (logical != 0x4c && logical != 0xd2c && logical != 0xd28 && logical != 0x68))
		return -EINVAL;
	return n1x_dla_register_offset(N1X_DLA_MAP_WINDOWS_1, logical,
				       endpoint->bytes, offset);
}

static int n1x_boot_read(void *context, u32 logical, u32 *value)
{
	struct n1x_boot_endpoint *endpoint = context;
	u32 offset;
	int err = n1x_boot_offset(endpoint, logical, false, &offset);

	if (err)
		return err;
	*value = readl(endpoint->base + offset);
	return 0;
}

static int n1x_boot_write(void *context, u32 logical, u32 value)
{
	struct n1x_boot_endpoint *endpoint = context;
	u32 offset;
	int err = n1x_boot_offset(endpoint, logical, true, &offset);

	if (err)
		return err;
	writel(value, endpoint->base + offset);
	return 0;
}

static void n1x_boot_delay(void *context, u32 interval_us)
{
	(void)context;
	usleep_range(interval_us, interval_us + 100);
}

static int n1x_boot_validate_firmware(const struct firmware *firmware)
{
	static const u8 expected[SHA256_DIGEST_SIZE] = {
		0x0f, 0x16, 0x91, 0x1c, 0xce, 0xd7, 0xbb, 0x95,
		0x77, 0x3d, 0xa5, 0xaf, 0xf9, 0xd9, 0xa4, 0x4d,
		0xfd, 0x1b, 0x1a, 0x11, 0xf3, 0xc5, 0x60, 0xee,
		0x69, 0x8a, 0x5c, 0x99, 0x31, 0x0f, 0xf2, 0xc4,
	};
	u8 digest[SHA256_DIGEST_SIZE];

	if (!firmware || !firmware->data || firmware->size != N1X_BOOT_BYTES)
		return -EBADMSG;
	sha256(firmware->data, firmware->size, digest);
	return memcmp(digest, expected, sizeof(digest)) ? -EBADMSG : 0;
}

/* Caller holds boot_lock. This function is the only upload entry point. */
static int n1x_boot_upload(void)
{
	const struct firmware *firmware;
	struct n1x_dla_io sroot_io, dla_io;
	u32 words[3];
	int err;

	if (!enable_upload || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla)
		return -ENODEV;
	if (boot_attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_sroot);
	if (!err)
		err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = request_firmware_direct(&firmware, N1X_BOOT_FIRMWARE, boot_sroot->dev);
	if (err)
		return err;
	err = n1x_boot_validate_firmware(firmware);
	if (err)
		goto release;
	err = dma_set_mask_and_coherent(boot_sroot->dev, DMA_BIT_MASK(32));
	if (err)
		goto release;
	err = pm_runtime_resume_and_get(boot_sroot->dev);
	if (err < 0)
		goto release;
	err = pm_runtime_resume_and_get(boot_dla->dev);
	if (err < 0)
		goto put_sroot;
	boot_buffer = dma_alloc_coherent(boot_sroot->dev, N1X_BOOT_BYTES,
					 &boot_dma, GFP_KERNEL);
	if (!boot_buffer) {
		err = -ENOMEM;
		goto put_dla;
	}
	if (boot_dma & 255) {
		err = -ERANGE;
		goto free;
	}
	err = n1x_sroot_stage_image(boot_buffer, N1X_BOOT_BYTES,
				    firmware->data, firmware->size, boot_dma,
				    0xe0000000, 0xffffffff, words);
	if (err)
		goto free;
	/* Reference acquired BEFORE publication. No path releases it after this
	 * point, including timeout/rejection/idle failure. Unload is prohibited
	 * until reboot rather than guessing DMA quiescence.
	 */
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto free;
	}
	sroot_io = (struct n1x_dla_io){boot_sroot, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dla_io = (struct n1x_dla_io){boot_dla, NULL, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	boot_attempted = true;
	err = n1x_dla_sroot_upload(&sroot_io, &dla_io, 16, words, 5001, 1000, &boot_result);
	dev_info(boot_dla->dev,
		 "upload phase returned %d: reply_valid=%u raw=%#x idle=%u; DMA %pad retained until reboot\n",
		 err, boot_result.reply_valid, boot_result.reply.header,
		 boot_result.idle_observed, &boot_dma);
	/* The original firmware file is independent of its staged DMA copy. */
	release_firmware(firmware);
	return err;
free:
	dma_free_coherent(boot_sroot->dev, N1X_BOOT_BYTES, boot_buffer, boot_dma);
	boot_buffer = NULL;
	boot_dma = 0;
put_dla:
	pm_runtime_put(boot_dla->dev);
put_sroot:
	pm_runtime_put(boot_sroot->dev);
release:
	release_firmware(firmware);
	return err;
}

/* Caller holds boot_lock. The upload already pins module/runtime references.
 * A DMA-word change qualifies only command 1, never model execution/quiescence.
 */
static int n1x_boot_ping_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	u32 attempt;
	int err;

	if (!enable_ping || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_attempted ||
	    !boot_result.reply_valid || !boot_result.idle_observed ||
	    boot_result.reply.status || boot_result.reply.detail)
		return -ENODEV;
	if (boot_ping.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_ping.buffer = dma_alloc_coherent(boot_dla->dev, 4096,
					      &boot_ping.dma, GFP_KERNEL);
	if (!boot_ping.buffer)
		return -ENOMEM;
	if (!boot_ping.dma || (boot_ping.dma & 255) || boot_ping.dma > maximum ||
	    4095 > maximum - boot_ping.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_ping.buffer, 0, 4096);
	WRITE_ONCE(*(__le32 *)boot_ping.buffer, cpu_to_le32(0x11111111));
	err = n1x_boot_read(boot_dla, 0xd2c, &boot_ping.owned);
	if (err)
		goto free;
	err = n1x_boot_read(boot_dla, 0xd28, &boot_ping.mask_before);
	if (err)
		goto free;
	boot_ping.masked = boot_ping.owned & boot_ping.mask_before;
	/* Conservative lifetime from the first mask-write attempt. No retries,
	 * auto-restoration or freeing once the phase can change device state.
	 */
	boot_ping.attempted = true;
	err = n1x_boot_write(boot_dla, 0xd24, boot_ping.masked);
	if (err)
		return err;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	/* Original Windows direct wrapper receives flags=0: method 1, not 0x301.
	 * The result is in DMA memory; no mailbox ACK or IRQ handler is needed
	 * by the emulated success path. Physical behavior is still unqualified.
	 */
	err = n1x_dla_send_method(&io, 1, boot_ping.dma >> 8, 5001, 1000);
	if (err)
		return err;
	for (attempt = 0; attempt < 5001; attempt++) {
		boot_ping.word = le32_to_cpu(READ_ONCE(*(__le32 *)boot_ping.buffer));
		if (boot_ping.word == 0x44444444) {
			dma_rmb();
			boot_ping.verified = true;
			return 0;
		}
		if (attempt + 1 < 5001)
			n1x_boot_delay(boot_dla, 1000);
	}
	return -ETIMEDOUT;
free:
	dma_free_coherent(boot_dla->dev, 4096, boot_ping.buffer, boot_ping.dma);
	boot_ping.buffer = NULL;
	boot_ping.dma = 0;
	return err;
}

static ssize_t ping_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "ping"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_ping_once();
	boot_ping.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(ping);

static ssize_t upload_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "upload"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_upload();
	boot_error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(upload);

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;

	mutex_lock(&boot_lock);
	size = sysfs_emit(buf,
		"dla_bound=%u sroot_bound=%u enabled=%u attempted=%u error=%d reply_valid=%u reply=%#x idle_observed=%u dma_retained=%u ping_enabled=%u ping_attempted=%u ping_error=%d ping_verified=%u ping_word=%#x ping_retained=%u\n",
		!!boot_dla, !!boot_sroot, enable_upload, boot_attempted, boot_error,
		boot_result.reply_valid, boot_result.reply.header,
		boot_result.idle_observed, !!boot_buffer, enable_ping, boot_ping.attempted,
		boot_ping.error, boot_ping.verified, boot_ping.word, !!boot_ping.buffer);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_status = __ATTR(status, 0400, status_show, NULL);
static struct attribute *boot_attrs[] = {&dev_attr_upload.attr, &dev_attr_ping.attr, &dev_attr_status.attr, NULL};
ATTRIBUTE_GROUPS(boot);

static int n1x_boot_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct n1x_boot_endpoint *endpoint;
	struct resource *first, *second;
	struct resource registers = {.flags = IORESOURCE_MEM};
	unsigned long long revision;
	bool sroot;
	int err = 0;

	if (!n1x_boot_machine() || !adev ||
	    (strcmp(acpi_device_hid(adev), "NVDA8600") &&
	     strcmp(acpi_device_hid(adev), "NVDA200A")))
		return -ENODEV;
	sroot = !strcmp(acpi_device_hid(adev), "NVDA8600");
	if (ACPI_FAILURE(acpi_evaluate_integer(adev->handle, "_HRV", NULL, &revision)) ||
	    revision != (sroot ? 1 : 0x34))
		return -ENODEV;
	first = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	second = platform_get_resource(pdev, IORESOURCE_MEM, 1);
	if (!first || !second || platform_get_resource(pdev, IORESOURCE_MEM, 2) ||
	    first->start != (sroot ? 0x18010000 : 0x19000000) ||
	    resource_size(first) != (sroot ? 0x1000 : 0x40000) ||
	    second->start != (sroot ? 0x18010000 : 0x31380000) ||
	    resource_size(second) != (sroot ? 0x10 : 0x10000))
		return -ENODEV;
	endpoint = devm_kzalloc(dev, sizeof(*endpoint), GFP_KERNEL);
	if (!endpoint)
		return -ENOMEM;
	endpoint->dev = dev;
	endpoint->sroot = sroot;
	registers.start = first->start;
	registers.end = first->end;
	registers.name = dev_name(dev);
	if (sroot) {
		endpoint->first = 16;
		registers.start += 16;
		registers.end = first->start + 12 * 16 + 3;
	}
	endpoint->bytes = resource_size(&registers);
	endpoint->base = devm_ioremap_resource(dev, &registers);
	if (IS_ERR(endpoint->base))
		return PTR_ERR(endpoint->base);
	mutex_lock(&boot_lock);
	if (sroot ? !!boot_sroot : !!boot_dla) {
		err = -EBUSY;
	} else {
		if (sroot)
			boot_sroot = endpoint;
		else
			boot_dla = endpoint;
		platform_set_drvdata(pdev, endpoint);
	}
	mutex_unlock(&boot_lock);
	return err;
}

static void n1x_boot_remove(struct platform_device *pdev)
{
	mutex_lock(&boot_lock);
	if (boot_sroot && boot_sroot->dev == &pdev->dev)
		boot_sroot = NULL;
	if (boot_dla && boot_dla->dev == &pdev->dev)
		boot_dla = NULL;
	mutex_unlock(&boot_lock);
}

static int n1x_boot_suspend(struct device *dev)
{
	int err;

	mutex_lock(&boot_lock);
	err = boot_attempted ? -EBUSY : 0;
	mutex_unlock(&boot_lock);
	return err;
}
static const struct dev_pm_ops n1x_boot_pm = {
	.suspend = n1x_boot_suspend,
	.freeze = n1x_boot_suspend,
	.poweroff = n1x_boot_suspend,
};
static const struct acpi_device_id n1x_boot_ids[] = {{"NVDA200A", 0}, {"NVDA8600", 0}, {}};
static struct platform_driver n1x_boot_driver = {
	.probe = n1x_boot_probe,
	.remove = n1x_boot_remove,
	.driver = {
		.name = "n1x-npu-bootstrap",
		.acpi_match_table = n1x_boot_ids,
		.dev_groups = boot_groups,
		.suppress_bind_attrs = true,
		.pm = &n1x_boot_pm,
	},
};

static int __init n1x_boot_init(void)
{
	if (!n1x_boot_machine())
		return -ENODEV;
	return platform_driver_register(&n1x_boot_driver);
}
module_init(n1x_boot_init);
static void __exit n1x_boot_exit(void)
{
	platform_driver_unregister(&n1x_boot_driver);
}
module_exit(n1x_boot_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Explicit one-attempt P16 SROOT upload qualification; reboot required after attempt");
MODULE_FIRMWARE(N1X_BOOT_FIRMWARE);
