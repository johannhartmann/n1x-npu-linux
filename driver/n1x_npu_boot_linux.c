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
#include "n1x_dla_control.h"
#include "n1x_dla_registers.h"
#include "n1x_dla_task.h"
#include "n1x_hfrp_setup.h"

#define N1X_BOOT_KERNEL "7.2.8-23-omarchy-p16-asus-npu2"
#define N1X_BOARD_FIRMWARE "nvidia/n1x/p16-bios300-physical.rom"
#define N1X_BOOT_FIRMWARE "nvidia/n1x/p16-sroot-v0.bin"
#define N1X_BOOT_FALLBACK_FIRMWARE "nvidia/n1x/p16-sroot-v1.bin"
#define N1X_BOOT_BYTES 67508
#define N1X_REGION5_ALLOC (1U << 20)
#define N1X_REGION5_OFFSET 16U
#define N1X_REGION5_STRIDE 52U
#define N1X_REGION5_BYTES (((N1X_REGION5_ALLOC - N1X_REGION5_OFFSET - 8U) / \
			   N1X_REGION5_STRIDE) * N1X_REGION5_STRIDE + 8U)

static bool enable_upload;
module_param(enable_upload, bool, 0400);
MODULE_PARM_DESC(enable_upload, "Permit root's explicit one-attempt upload trigger (default false)");

static bool enable_hfrp;
module_param(enable_hfrp, bool, 0400);
MODULE_PARM_DESC(enable_hfrp, "Permit one explicit board-policy HFRP prepare/power-on attempt (default false)");

static unsigned int firmware_variant;
module_param(firmware_variant, uint, 0400);
MODULE_PARM_DESC(firmware_variant, "Pinned remapper-1 image: 0=initial, 1=Windows fallback; no automatic retry (default 0)");

static bool enable_ping;
module_param(enable_ping, bool, 0400);
MODULE_PARM_DESC(enable_ping, "Permit one explicit DMA-word ping after successful upload phase (default false)");

static bool enable_config_snapshot;
module_param(enable_config_snapshot, bool, 0400);
MODULE_PARM_DESC(enable_config_snapshot, "Permit one explicit command-22 snapshot after DMA ping (default false)");

static bool enable_region5;
module_param(enable_region5, bool, 0400);
MODULE_PARM_DESC(enable_region5, "Permit one explicit region-5 initialization after DMA ping (default false)");

static bool enable_config_apply;
module_param(enable_config_apply, bool, 0400);
MODULE_PARM_DESC(enable_config_apply, "Permit one explicit default config import after export/region-5 qualification (default false)");

static bool enable_lpwr_config;
module_param(enable_lpwr_config, bool, 0400);
MODULE_PARM_DESC(enable_lpwr_config, "Permit one explicit command-23 Windows fallback config import (default false)");

static bool enable_bypass_task;
module_param(enable_bypass_task, bool, 0400);
MODULE_PARM_DESC(enable_bypass_task, "Permit one explicit single bypass task after config qualification (default false)");

struct n1x_boot_endpoint {
	struct device *dev;
	void __iomem *base;
	void __iomem *hfrp;
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
	struct n1x_hfrp_channel channel;
	struct n1x_hfrp_version version;
	struct n1x_hfrp_exchange_result exchange;
	struct n1x_hfrp_setup_result setup;
	struct n1x_dla_vbios_policy policy;
	struct n1x_dla_gating_policy gating;
	struct n1x_dla_vbios_location location;
	bool attempted, powered;
	u32 phase;
	int error;
} boot_hfrp;
static struct {
	void *buffer;
	dma_addr_t dma;
	u32 owned, mask_before, masked, word;
	bool attempted, verified;
	int error;
} boot_ping;
static struct {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	u8 data[252];
	bool attempted, valid;
	int error;
} boot_config;
static struct {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	bool attempted, registered;
	int error;
} boot_region5;
static struct {
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
} boot_config_apply;

static struct {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
} boot_lpwr;

static struct {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_response response;
	u32 pending, fence;
	bool attempted, response_valid, accepted, signal_seen, verified;
	int error;
} boot_task;

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
	/* Upload has no DLA writer. Opt-in ping/control phases use only recovered
	 * route/mask/method/mailbox registers; no core-start or reset.
	 */
	if (write ? (logical != 0xd24 && logical != 0x6c && logical != 0x68 &&
		     logical != 4 && logical != 0x40) :
	    (logical != 0x4c && logical != 0xd2c && logical != 0xd28 && logical != 0x68 &&
	     logical != 8 && logical != 0x40 && logical != 0x44))
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

/* Separate, bounded HFRP resource. Never pass these offsets through the DLA
 * register remapper. No IRQ handler consumes these rings concurrently.
 */
static bool n1x_boot_hfrp_allowed(u32 offset, bool write)
{
	if (offset & 3)
		return false;
	if (write)
		return offset == 4 || offset == 0x100 || offset == 0x10c ||
		       (offset >= 0x110 && offset <= 0x184);
	return offset == 0 || offset == 0x100 || offset == 0x104 ||
	       (offset >= 0x110 && offset <= 0x1fc);
}

static int n1x_boot_hfrp_read(void *context, u32 offset, u32 *value)
{
	struct n1x_boot_endpoint *endpoint = context;
	if (!endpoint->hfrp || !n1x_boot_hfrp_allowed(offset, false))
		return -EINVAL;
	*value = readl(endpoint->hfrp + offset);
	return 0;
}

static int n1x_boot_hfrp_write(void *context, u32 offset, u32 value)
{
	struct n1x_boot_endpoint *endpoint = context;
	if (!endpoint->hfrp || !n1x_boot_hfrp_allowed(offset, true))
		return -EINVAL;
	writel(value, endpoint->hfrp + offset);
	return 0;
}

/* Verify the complete immutable capture before parsing; synthetic/default
 * policy is never accepted as a hardware input. This file is locally acquired,
 * not redistributed vendor firmware. */
static int n1x_boot_board_policy(const struct firmware *firmware)
{
	static const u8 expected[SHA256_DIGEST_SIZE] = {
		0xa8, 0x2a, 0xf1, 0x4d, 0x15, 0xef, 0xfa, 0x98,
		0xa7, 0x5b, 0x19, 0xd9, 0x13, 0x54, 0x21, 0xc7,
		0xc2, 0x7f, 0x20, 0xcc, 0xa7, 0x39, 0x8d, 0x0b,
		0x0a, 0x7b, 0xa8, 0x75, 0x47, 0xb8, 0xb6, 0x4c,
	};
	u8 digest[SHA256_DIGEST_SIZE];
	int err;
	if (!firmware || !firmware->data || firmware->size != 0x110000)
		return -EBADMSG;
	sha256(firmware->data, firmware->size, digest);
	if (memcmp(digest, expected, sizeof(digest)))
		return -EBADMSG;
	err = n1x_dla_parse_vbios_policy(firmware->data, firmware->size,
			&boot_hfrp.policy, &boot_hfrp.location);
	if (err)
		return err;
	return n1x_dla_prepare_gating(&boot_hfrp.policy, &boot_hfrp.gating);
}

/* Caller holds boot_lock. Hold PM/module lifetime from BEFORE the first write,
 * on failure as well as success. No retry, reset, IRQ enable or SROOT upload.
 * Unknown asynchronous responses fail closed in the common channel code.
 */
static int n1x_boot_prepare_hfrp_once(void)
{
	const struct firmware *firmware;
	struct n1x_dla_io io;
	u32 stamp;
	int err;
	if (!enable_hfrp || !n1x_boot_machine())
		return -EPERM;
	if (!boot_dla || !boot_dla->hfrp || !boot_sroot)
		return -ENODEV;
	if (boot_hfrp.attempted || boot_attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (!err)
		err = n1x_boot_dma_gate(boot_sroot);
	if (err)
		return err;
	err = request_firmware_direct(&firmware, N1X_BOARD_FIRMWARE, boot_dla->dev);
	if (err)
		return err;
	err = n1x_boot_board_policy(firmware);
	release_firmware(firmware);
	if (err)
		return err;
	err = pm_runtime_resume_and_get(boot_dla->dev);
	if (err < 0)
		return err;
	err = n1x_boot_hfrp_read(boot_dla, 0, &stamp);
	if (!err && stamp != 0x20251722)
		err = -EOPNOTSUPP;
	if (err)
		goto put;
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto put;
	}
	io = (struct n1x_dla_io){boot_dla, n1x_boot_hfrp_write,
				n1x_boot_hfrp_read, n1x_boot_delay};
	boot_hfrp.attempted = true;
	boot_hfrp.phase = 1;
	err = n1x_hfrp_handshake(&io, &boot_hfrp.channel, 5001, 1000, &boot_hfrp.exchange);
	if (err)
		goto done;
	boot_hfrp.phase = 2;
	err = n1x_hfrp_discover_version(&io, &boot_hfrp.channel, 5001, 1000,
			&boot_hfrp.version, &boot_hfrp.exchange);
	if (err)
		goto done;
	boot_hfrp.phase = 3;
	err = n1x_hfrp_configure_policy(&io, &boot_hfrp.channel, &boot_hfrp.version,
			&boot_hfrp.policy, 5001, 1000, &boot_hfrp.setup);
	if (err)
		goto done;
	boot_hfrp.phase = 4;
	err = n1x_hfrp_power_on(&io, &boot_hfrp.channel, &boot_hfrp.version,
			boot_hfrp.gating.policy_bytes[2], 5001, 1000, &boot_hfrp.exchange);
	if (!err) {
		boot_hfrp.powered = true;
		boot_hfrp.phase = 5;
	}
done:
	dev_info(boot_dla->dev,
		 "HFRP phase=%u error=%d version=%u.%u command=%u completed=%u setup_reply=%#x/%u final_reply=%#x/%u power_accepted=%u; retained until reboot\n",
		 boot_hfrp.phase, err, boot_hfrp.version.major, boot_hfrp.version.minor,
		 boot_hfrp.setup.command, boot_hfrp.setup.completed_commands,
		 boot_hfrp.setup.exchange.reply.header, boot_hfrp.setup.exchange.reply.length,
		 boot_hfrp.exchange.reply.header, boot_hfrp.exchange.reply.length, boot_hfrp.powered);
	return err;
put:
	pm_runtime_put(boot_dla->dev);
	return err;
}

static int n1x_boot_validate_firmware(const struct firmware *firmware)
{
	static const u8 expected[2][SHA256_DIGEST_SIZE] = {{
		0x0f, 0x16, 0x91, 0x1c, 0xce, 0xd7, 0xbb, 0x95,
		0x77, 0x3d, 0xa5, 0xaf, 0xf9, 0xd9, 0xa4, 0x4d,
		0xfd, 0x1b, 0x1a, 0x11, 0xf3, 0xc5, 0x60, 0xee,
		0x69, 0x8a, 0x5c, 0x99, 0x31, 0x0f, 0xf2, 0xc4,
	}, {
		0x6b, 0x1e, 0x81, 0x92, 0xfc, 0x1e, 0x61, 0x94,
		0x4e, 0x9b, 0x2a, 0x84, 0x25, 0x1b, 0x7d, 0x18,
		0xf3, 0x05, 0x02, 0x5f, 0xfb, 0x2c, 0xfa, 0x1d,
		0xe8, 0x67, 0x7f, 0x8f, 0x98, 0xb5, 0x9a, 0xd4,
	}};
	u8 digest[SHA256_DIGEST_SIZE];

	if (firmware_variant > 1)
		return -EINVAL;
	if (!firmware || !firmware->data || firmware->size != N1X_BOOT_BYTES)
		return -EBADMSG;
	sha256(firmware->data, firmware->size, digest);
	return memcmp(digest, expected[firmware_variant], sizeof(digest)) ? -EBADMSG : 0;
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
	if (!boot_hfrp.powered)
		return -EHOSTDOWN;
	if (firmware_variant > 1)
		return -EINVAL;
	err = n1x_boot_dma_gate(boot_sroot);
	if (!err)
		err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = request_firmware_direct(&firmware, firmware_variant ?
				      N1X_BOOT_FALLBACK_FIRMWARE : N1X_BOOT_FIRMWARE,
				      boot_sroot->dev);
	if (err)
		return err;
	err = n1x_boot_validate_firmware(firmware);
	if (err)
		goto release;
	err = dma_set_mask_and_coherent(boot_sroot->dev, DMA_BIT_MASK(32));
	if (err)
		goto release;
	err = pm_runtime_resume_and_get(boot_sroot->dev);
	if (err < 0) {
		dev_err(boot_sroot->dev, "runtime resume before upload failed: %d\n", err);
		goto release;
	}
	err = pm_runtime_resume_and_get(boot_dla->dev);
	if (err < 0) {
		dev_err(boot_dla->dev, "runtime resume before upload failed: %d\n", err);
		goto put_sroot;
	}
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
		 "upload variant %u phase returned %d: reply_valid=%u raw=%#x idle=%u; DMA %pad retained until reboot\n",
		 firmware_variant, err, boot_result.reply_valid, boot_result.reply.header,
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

/* Caller holds boot_lock; snapshot export is not configuration mutation. */
static int n1x_boot_read_config_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!enable_config_snapshot || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified)
		return -ENODEV;
	if (boot_config.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_config.buffer = dma_alloc_coherent(boot_dla->dev, 4096,
						&boot_config.dma, GFP_KERNEL);
	if (!boot_config.buffer)
		return -ENOMEM;
	if (!boot_config.dma || (boot_config.dma & 255) || boot_config.dma > maximum ||
	    4095 > maximum - boot_config.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_config.buffer, 0, 4096);
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	/* Retain conservatively even on a stale-response/pre-send error. The
	 * preceding upload already pins module/runtime references until reboot.
	 */
	boot_config.attempted = true;
	err = n1x_dla_control_command(&io, 22, boot_config.dma, 5001, 1000,
				       &boot_config.transaction);
	if (err)
		return err;
	dma_rmb();
	memcpy(boot_config.data, boot_config.buffer, sizeof(boot_config.data));
	boot_config.valid = true;
	return 0;
free:
	dma_free_coherent(boot_dla->dev, 4096, boot_config.buffer, boot_config.dma);
	boot_config.buffer = NULL;
	boot_config.dma = 0;
	return err;
}

/* Caller holds boot_lock; exactly the observed Windows region-5 allocation
 * layout, without a ring consumer, task submission or configuration import.
 */
static int n1x_boot_init_region5_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!enable_region5 || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified)
		return -ENODEV;
	if (boot_region5.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_region5.buffer = dma_alloc_coherent(boot_dla->dev, N1X_REGION5_ALLOC,
						 &boot_region5.dma, GFP_KERNEL);
	if (!boot_region5.buffer)
		return -ENOMEM;
	if (!boot_region5.dma || (boot_region5.dma & 255) || boot_region5.dma > maximum ||
	    N1X_REGION5_ALLOC - 1 > maximum - boot_region5.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_region5.buffer, 0, N1X_REGION5_ALLOC);
	err = n1x_dla_build_region(boot_region5.buffer, N1X_REGION5_ALLOC, 5,
				    boot_region5.dma + N1X_REGION5_OFFSET, N1X_REGION5_BYTES);
	if (err)
		goto free;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	/* The existing upload reference owns module/runtime lifetime. Do not
	 * release this descriptor/ring even after an accepted/ACKed command.
	 */
	boot_region5.attempted = true;
	err = n1x_dla_control_command(&io, 11, boot_region5.dma, 5001, 1000,
				       &boot_region5.transaction);
	if (!err)
		boot_region5.registered = true;
	return err;
free:
	dma_free_coherent(boot_dla->dev, N1X_REGION5_ALLOC,
			  boot_region5.buffer, boot_region5.dma);
	boot_region5.buffer = NULL;
	boot_region5.dma = 0;
	return err;
}

static ssize_t init_region5_store(struct device *dev, struct device_attribute *attr,
				  const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "init"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_init_region5_once();
	boot_region5.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(init_region5);

static ssize_t read_config_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "read"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_read_config_once();
	boot_config.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(read_config);

/* Reuse the already retained config allocation, as the Windows init sequence
 * does. The saved CPU snapshot remains the original pre-import export.
 */
static int n1x_boot_apply_config_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!enable_config_apply || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified || !boot_region5.registered ||
	    !boot_config.valid || !boot_config.buffer)
		return -ENODEV;
	if (boot_config_apply.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	if (!boot_config.dma || (boot_config.dma & 255) || boot_config.dma > maximum ||
	    4095 > maximum - boot_config.dma)
		return -ERANGE;
	err = n1x_dla_prepare_default_config(boot_config.buffer, 4096);
	if (err)
		return err;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	boot_config_apply.attempted = true;
	err = n1x_dla_control_command(&io, 21, boot_config.dma, 5001, 1000,
				       &boot_config_apply.transaction);
	if (!err)
		boot_config_apply.accepted = true;
	/* No path frees or replaces the export's already-published allocation. */
	return err;
}

static ssize_t apply_config_store(struct device *dev, struct device_attribute *attr,
				  const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "apply"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_apply_config_once();
	boot_config_apply.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(apply_config);

/* Windows setup uses provider +0xf4=4 only when the original field is zero.
 * This explicit experiment imports that recovered fallback, not a measured
 * live Windows policy. Do not interpret the field as a physical time unit.
 */
static int n1x_boot_apply_lpwr_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!enable_lpwr_config || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified ||
	    !boot_region5.registered || !boot_config_apply.accepted)
		return -ENODEV;
	if (boot_lpwr.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_lpwr.buffer = dma_alloc_coherent(boot_dla->dev, 4096,
					    &boot_lpwr.dma, GFP_KERNEL);
	if (!boot_lpwr.buffer)
		return -ENOMEM;
	if (!boot_lpwr.dma || (boot_lpwr.dma & 255) || boot_lpwr.dma > maximum ||
	    4095 > maximum - boot_lpwr.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_lpwr.buffer, 0, 4096);
	err = n1x_dla_build_command23(boot_lpwr.buffer, 4096, 1, 1, 4);
	if (err)
		goto free;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	/* Upload already holds runtime/module references. A reply or ACK cannot
	 * establish quiescence: retain this backing on every attempted command.
	 */
	boot_lpwr.attempted = true;
	err = n1x_dla_control_command(&io, 23, boot_lpwr.dma, 5001, 1000,
				       &boot_lpwr.transaction);
	if (!err)
		boot_lpwr.accepted = true;
	return err;
free:
	dma_free_coherent(boot_dla->dev, 4096, boot_lpwr.buffer, boot_lpwr.dma);
	boot_lpwr.buffer = NULL;
	boot_lpwr.dma = 0;
	return err;
}

static ssize_t apply_lpwr_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "apply"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_apply_lpwr_once();
	boot_lpwr.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(apply_lpwr);

/* One owned diagnostic descriptor, not an arbitrary model/task UAPI.
 * A changed fence alone is insufficient; require submission acceptance and
 * SWGEN0, processing pending mailbox errors first. Never infer DMA quiescence.
 */
static int n1x_boot_bypass_task_once(void)
{
	struct n1x_dla_response_fields fields;
	struct n1x_dla_task_args args = {0};
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36), network;
	u32 attempt;
	int err;

	if (!enable_bypass_task || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified ||
	    !boot_region5.registered || !boot_config_apply.accepted || !boot_lpwr.accepted)
		return -ENODEV;
	if (boot_task.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot_dla);
	if (err)
		return err;
	err = n1x_boot_read(boot_dla, 8, &boot_task.pending);
	if (err)
		return err;
	if (boot_task.pending & 0xc0)
		return -EBUSY;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_task.buffer = dma_alloc_coherent(boot_dla->dev, 4096,
					    &boot_task.dma, GFP_KERNEL);
	if (!boot_task.buffer)
		return -ENOMEM;
	if (!boot_task.dma || (boot_task.dma & 255) || boot_task.dma > maximum ||
	    4095 > maximum - boot_task.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_task.buffer, 0, 4096);
	args.address_list = boot_task.dma + 256;
	args.num_addresses = 1;
	network = boot_task.dma + 512;
	err = n1x_dla_build_address_list(boot_task.buffer + 256, 256, &network, 1);
	if (!err)
		err = n1x_dla_build_bypass_fenced_task_v2(boot_task.buffer, 256, &args,
						      boot_task.dma + 768, 0x12345678);
	if (err)
		goto free;
	WRITE_ONCE(*(__le32 *)(boot_task.buffer + 768), cpu_to_le32(0xdeadbeef));
	boot_task.fence = 0xdeadbeef;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	boot_task.attempted = true;
	err = n1x_dla_send_method(&io, 0x307, boot_task.dma >> 8, 5001, 1000);
	if (err)
		return err;
	for (attempt = 0; attempt < 5001; attempt++) {
		err = n1x_boot_read(boot_dla, 8, &boot_task.pending);
		if (err)
			return err;
		if (boot_task.pending & 0x80) {
			err = n1x_dla_take_response(&io, &boot_task.response);
			if (err)
				return err;
			boot_task.response_valid = true;
			err = n1x_dla_decode_response(&boot_task.response, &fields);
			if (err)
				return err;
			if (fields.command != 7 || fields.data != boot_task.dma >> 8)
				return -EPROTO;
			if (fields.status || fields.message == 1 || fields.message == 8 ||
			    fields.message == 9 || fields.message == 10)
				return -EREMOTEIO;
			if (fields.upper || fields.message != 2 || boot_task.accepted)
				return -EPROTO;
			boot_task.accepted = true;
		}
		if (boot_task.pending & 0x40)
			boot_task.signal_seen = true;
		dma_rmb();
		boot_task.fence = le32_to_cpu(READ_ONCE(*(__le32 *)(boot_task.buffer + 768)));
		if (boot_task.accepted && boot_task.signal_seen && boot_task.fence == 0x12345678) {
			/* A later error can coexist with a written fence. Recheck before
			 * acknowledging SWGEN0; leave new mailbox work for the next poll.
			 */
			err = n1x_boot_read(boot_dla, 8, &boot_task.pending);
			if (err)
				return err;
			if (!(boot_task.pending & 0x80)) {
				err = n1x_boot_write(boot_dla, 4, 0x40);
				if (err)
					return err;
				boot_task.verified = true;
				return 0;
			}
		}
		if (attempt + 1 < 5001)
			n1x_boot_delay(boot_dla, 1000);
	}
	return -ETIMEDOUT;
free:
	dma_free_coherent(boot_dla->dev, 4096, boot_task.buffer, boot_task.dma);
	boot_task.buffer = NULL;
	boot_task.dma = 0;
	return err;
}

static ssize_t bypass_task_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "run"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_bypass_task_once();
	boot_task.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(bypass_task);

static ssize_t config_snapshot_show(struct device *dev, struct device_attribute *attr,
				    char *buf)
{
	ssize_t size = 0;
	unsigned int i;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev || !boot_config.valid) {
		size = -ENODATA;
	} else {
		for (i = 0; i < sizeof(boot_config.data); i++)
			size += sysfs_emit_at(buf, size, "%02x", boot_config.data[i]);
		size += sysfs_emit_at(buf, size, "\n");
	}
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_config_snapshot =
	__ATTR(config_snapshot, 0400, config_snapshot_show, NULL);

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
	else if (boot_attempted)
		err = -EALREADY; /* Preserve the first published operation's result. */
	else {
		err = n1x_boot_upload();
		boot_error = err;
	}
	mutex_unlock(&boot_lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(upload);

static ssize_t prepare_hfrp_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	int err;
	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "prepare"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else if (boot_hfrp.attempted)
		err = -EALREADY;
	else {
		err = n1x_boot_prepare_hfrp_once();
		boot_hfrp.error = err;
	}
	mutex_unlock(&boot_lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(prepare_hfrp);

static ssize_t hfrp_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;
	mutex_lock(&boot_lock);
	size = sysfs_emit(buf,
		"enabled=%u attempted=%u phase=%u error=%d state=%u pending=%u stamp=%#x version=%u.%u configured=%u command=%u completed=%u setup_reply=%#x setup_length=%u setup_payload=%*phN final_reply=%#x final_length=%u final_payload=%*phN power_accepted=%u frequency_khz=%u gating=%#x\n",
		enable_hfrp, boot_hfrp.attempted, boot_hfrp.phase, boot_hfrp.error,
		boot_hfrp.channel.state, boot_hfrp.channel.pending_sequence,
		boot_hfrp.version.stamp, boot_hfrp.version.major, boot_hfrp.version.minor,
		boot_hfrp.setup.configured, boot_hfrp.setup.command, boot_hfrp.setup.completed_commands,
		boot_hfrp.setup.exchange.reply.header, boot_hfrp.setup.exchange.reply.length,
		(int)boot_hfrp.setup.exchange.reply.length, boot_hfrp.setup.exchange.reply.payload,
		boot_hfrp.exchange.reply.header, boot_hfrp.exchange.reply.length,
		(int)boot_hfrp.exchange.reply.length, boot_hfrp.exchange.reply.payload,
		boot_hfrp.powered, boot_hfrp.policy.frequency_khz, boot_hfrp.gating.request_mask);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_hfrp_status = __ATTR(hfrp_status, 0400, hfrp_status_show, NULL);

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;

	mutex_lock(&boot_lock);
	size = sysfs_emit(buf,
		"dla_bound=%u sroot_bound=%u enabled=%u attempted=%u error=%d reply_valid=%u reply=%#x idle_observed=%u dma_retained=%u ping_enabled=%u ping_attempted=%u ping_error=%d ping_verified=%u ping_word=%#x ping_retained=%u config_enabled=%u config_attempted=%u config_error=%d config_valid=%u config_reply_valid=%u config_reply=%#x config_retained=%u region5_enabled=%u region5_attempted=%u region5_error=%d region5_registered=%u region5_retained=%u config_apply_enabled=%u config_apply_attempted=%u config_apply_error=%d config_apply_accepted=%u config_apply_reply_valid=%u config_apply_reply=%#x lpwr_enabled=%u lpwr_attempted=%u lpwr_error=%d lpwr_accepted=%u lpwr_reply_valid=%u lpwr_reply=%#x lpwr_retained=%u task_enabled=%u task_attempted=%u task_error=%d task_response_valid=%u task_reply=%#x task_data=%#x task_accepted=%u task_signal_seen=%u task_fence=%#x task_pending=%#x task_verified=%u task_retained=%u\n",
		!!boot_dla, !!boot_sroot, enable_upload, boot_attempted, boot_error,
		boot_result.reply_valid, boot_result.reply.header,
		boot_result.idle_observed, !!boot_buffer, enable_ping, boot_ping.attempted,
		boot_ping.error, boot_ping.verified, boot_ping.word, !!boot_ping.buffer,
		enable_config_snapshot, boot_config.attempted, boot_config.error, boot_config.valid,
		boot_config.transaction.response_valid, boot_config.transaction.response.word,
		!!boot_config.buffer, enable_region5, boot_region5.attempted, boot_region5.error,
		boot_region5.registered, !!boot_region5.buffer, enable_config_apply,
		boot_config_apply.attempted, boot_config_apply.error, boot_config_apply.accepted,
		boot_config_apply.transaction.response_valid, boot_config_apply.transaction.response.word,
		enable_lpwr_config, boot_lpwr.attempted, boot_lpwr.error, boot_lpwr.accepted,
		boot_lpwr.transaction.response_valid, boot_lpwr.transaction.response.word, !!boot_lpwr.buffer,
		enable_bypass_task, boot_task.attempted, boot_task.error, boot_task.response_valid,
		boot_task.response.word, boot_task.response.data, boot_task.accepted, boot_task.signal_seen,
		boot_task.fence, boot_task.pending, boot_task.verified, !!boot_task.buffer);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_status = __ATTR(status, 0400, status_show, NULL);
static struct attribute *boot_attrs[] = {&dev_attr_upload.attr, &dev_attr_ping.attr, &dev_attr_read_config.attr,
	&dev_attr_prepare_hfrp.attr, &dev_attr_hfrp_status.attr,
	&dev_attr_config_snapshot.attr, &dev_attr_init_region5.attr,
	&dev_attr_apply_config.attr, &dev_attr_apply_lpwr.attr, &dev_attr_bypass_task.attr, &dev_attr_status.attr, NULL};
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
	if (!sroot) {
		/* Only the decoded transport's first 512 bytes, not the full 64 KiB. */
		registers.start = second->start;
		registers.end = second->start + 0x1ff;
		endpoint->hfrp = devm_ioremap_resource(dev, &registers);
		if (IS_ERR(endpoint->hfrp))
			return PTR_ERR(endpoint->hfrp);
	}
	/* Platform devices start with runtime PM disabled. Keep the normal
	 * subsystem resume path; do not force ACTIVE or skip its callbacks.
	 * Devres disables PM on failed probe or removal before any upload.
	 */
	err = devm_pm_runtime_enable(dev);
	if (err)
		return err;
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
	err = (boot_attempted || boot_hfrp.attempted) ? -EBUSY : 0;
	mutex_unlock(&boot_lock);
	return err;
}
static int n1x_boot_runtime_suspend(struct device *dev)
{
	/* No device quiescence/power-down protocol has been established. */
	return -EBUSY;
}

static const struct dev_pm_ops n1x_boot_pm = {
	.runtime_suspend = n1x_boot_runtime_suspend,
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
	if (firmware_variant > 1)
		return -EINVAL;
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
MODULE_FIRMWARE(N1X_BOOT_FALLBACK_FIRMWARE);
MODULE_FIRMWARE(N1X_BOARD_FIRMWARE);
