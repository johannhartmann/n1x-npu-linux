// SPDX-License-Identifier: GPL-2.0-only
/* NVIDIA N1X (GB10/T264) NPU Linux kernel driver (NVDA200A DLA + NVDA8600 SROOT).
 * Performs in-kernel HFRP power/clock bring-up, SROOT firmware upload, region/LPWR
 * initialization, and exposes the /dev/accel/accel* DRM compute accelerator node.
 * Retains DMA allocations across uncertain hardware faults (fail-closed).
 */
#include <crypto/sha2.h>
#include <linux/acpi.h>
#include <linux/capability.h>
#include <linux/component.h>
#include <linux/delay.h>
#include <linux/dma-map-ops.h>
#include <linux/dma-mapping.h>
#include <linux/dmi.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/interrupt.h>
#include <linux/iommu.h>
#include <linux/iommu-dma.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/wait.h>
#include "n1x_dla_boot.h"
#include "n1x_dla_command.h"
#include "n1x_dla_control.h"
#include "n1x_dla_registers.h"
#include "n1x_dla_task.h"
#include "n1x_dla_job.h"
#include "n1x_dla_job_control.h"
#include "n1x_dla_accel.h"
#include "n1x_hfrp_setup.h"

#define N1X_BOARD_FIRMWARE "nvidia/n1x/p16-bios300-physical.rom"
#define N1X_BOOT_FIRMWARE "nvidia/n1x/p16-sroot-v0.bin"
#define N1X_BOOT_FALLBACK_FIRMWARE "nvidia/n1x/p16-sroot-v1.bin"
#define N1X_BOOT_BYTES 67508
#define N1X_REGION5_ALLOC (1U << 20)
#define N1X_REGION5_OFFSET 16U
#define N1X_REGION5_STRIDE 52U
#define N1X_REGION5_BYTES (((N1X_REGION5_ALLOC - N1X_REGION5_OFFSET - 8U) / \
				N1X_REGION5_STRIDE) * N1X_REGION5_STRIDE + 8U)
/* GB10/T264 display controller register holding the 4 KiB-shifted VBIOS carveout offset. */
#define N1X_DISP_VBIOS_LOCATOR_PHYS 0x1182bc80ULL
#define N1X_VBIOS_CARVEOUT_BASE 0x80000600ULL
#define N1X_VBIOS_CARVEOUT_SIZE 0x110000U

static bool autostart = true;
module_param(autostart, bool, 0400);
MODULE_PARM_DESC(autostart, "Automatically run full NPU bring-up in C once both endpoints probe (default true)");

static bool enable_upload = true;
module_param(enable_upload, bool, 0400);
MODULE_PARM_DESC(enable_upload, "Permit SROOT firmware upload phase (default true)");

static bool enable_hfrp = true;
module_param(enable_hfrp, bool, 0400);
MODULE_PARM_DESC(enable_hfrp, "Permit board-policy HFRP prepare/power-on phase (default true)");

static unsigned int firmware_variant;
module_param(firmware_variant, uint, 0400);
MODULE_PARM_DESC(firmware_variant, "Pinned remapper-1 image: 0=initial, 1=Windows fallback; no automatic retry (default 0)");

static bool enable_ping = true;
module_param(enable_ping, bool, 0400);
MODULE_PARM_DESC(enable_ping, "Permit DMA-word ping after upload phase (default true)");

static bool enable_config_snapshot = true;
module_param(enable_config_snapshot, bool, 0400);
MODULE_PARM_DESC(enable_config_snapshot, "Permit command-22 config snapshot after DMA ping (default true)");

static bool enable_region5 = true;
module_param(enable_region5, bool, 0400);
MODULE_PARM_DESC(enable_region5, "Permit region-5 initialization after DMA ping (default true)");

static bool enable_config_apply = true;
module_param(enable_config_apply, bool, 0400);
MODULE_PARM_DESC(enable_config_apply, "Permit default config import after region-5 (default true)");

static bool enable_lpwr_config = true;
module_param(enable_lpwr_config, bool, 0400);
MODULE_PARM_DESC(enable_lpwr_config, "Permit command-23 board-policy LPWR config import (default true)");

static bool enable_bypass_task = true;
module_param(enable_bypass_task, bool, 0400);
MODULE_PARM_DESC(enable_bypass_task, "Permit initial bypass task verification (default true)");

static bool enable_accel = true;
module_param(enable_accel, bool, 0400);
MODULE_PARM_DESC(enable_accel, "Register /dev/accel/accel* DRM accelerator device (default true)");

static bool strict_platform;
module_param(strict_platform, bool, 0400);
MODULE_PARM_DESC(strict_platform, "Require exact P16 BIOS-300 DMI/kernel/ROM-hash pins (default false; dynamically discovers VBIOS policy, ACPI _CRS, and IORT SMMU)");

static bool opt_enable_irq = true;
module_param_named(enable_irq, opt_enable_irq, bool, 0400);
MODULE_PARM_DESC(enable_irq, "Enable Windows KMD (0xd49800) SWGEN0/SWGEN1 interrupt mask state machine and waitqueue wakeup (default true)");

static bool enable_preempt = true;
module_param(enable_preempt, bool, 0400);
MODULE_PARM_DESC(enable_preempt, "Permit HAL-1 preemption enablement (0x31c) and resumable job control (default true)");

static bool debug_sysfs;
module_param(debug_sysfs, bool, 0400);
MODULE_PARM_DESC(debug_sysfs, "Expose step-by-step qualification sysfs attributes (default false; automatically enabled when autostart=0)");

static DECLARE_WAIT_QUEUE_HEAD(boot_waitq);
static atomic_t boot_irq_count = ATOMIC_INIT(0);
static bool boot_irq_registered;
static struct {
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
} boot_preempt;

struct n1x_boot_endpoint {
	struct device *dev;
	void __iomem *base;
	void __iomem *hfrp;
	resource_size_t bytes;
	u32 first;
	bool sroot;
	struct n1x_dla_accel *accel;
};

static DEFINE_MUTEX(boot_lock);
static struct n1x_boot_endpoint *boot_dla, *boot_sroot;
static struct n1x_dla_sroot_load_result boot_result;
static void *boot_buffer;
static dma_addr_t boot_dma;
static int boot_error;
static bool boot_attempted;
static bool boot_stopping, boot_hfrp_held, boot_upload_held, boot_unpinned;
static struct {
	struct n1x_hfrp_stop_result transport;
	const char *stage;
	u32 irq_owned, irq_mask_before, irq_mask_after;
	bool irq_readback_valid;
	bool attempted, hardware_off, released;
	int error;
} boot_shutdown;
static struct {
	struct n1x_hfrp_channel channel;
	struct n1x_hfrp_version version;
	struct n1x_hfrp_exchange_result exchange;
	struct n1x_hfrp_setup_result setup;
	struct n1x_dla_vbios_policy policy;
	struct n1x_dla_gating_policy gating;
	struct n1x_dla_vbios_location location;
	struct n1x_hfrp_power_state power_state;
	struct n1x_hfrp_exchange_result state_exchange;
	bool state_attempted;
	int state_error;
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

static struct n1x_dla_job boot_task;
static struct n1x_dla_job_queue boot_queue;

static bool n1x_boot_machine(void)
{
	if (!strict_platform)
		return true;
	return dmi_match(DMI_SYS_VENDOR, "ASUS") &&
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
		!spec || (strict_platform ?
				(spec->num_ids != 1 || spec->ids[0] != (endpoint->sroot ? 1 : 0x2b)) :
				spec->num_ids < 1))
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

static int n1x_boot_write_irq_enable(struct n1x_boot_endpoint *endpoint, u32 value)
{
	u32 offset;
	int err;

	if (!endpoint || endpoint->sroot)
		return -EINVAL;
	err = n1x_dla_register_offset(N1X_DLA_MAP_WINDOWS_1, 0xd20,
						endpoint->bytes, &offset);
	if (err)
		return err;
	writel(value, endpoint->base + offset);
	return 0;
}

/* Windows KMD 0xd49800 top-half: mask owned active bits via 0xd24 to silence
 * the level-triggered GIC line (GSI 234) and wake any waiting poller.
 */
static irqreturn_t n1x_boot_irq_handler(int irq, void *data)
{
	struct n1x_boot_endpoint *endpoint = data;
	u32 pending = 0, owned = 0, saved = 0;

	(void)irq;
	if (!endpoint || endpoint->sroot ||
		!READ_ONCE(boot_hfrp.powered) || READ_ONCE(boot_stopping))
		return IRQ_NONE;
	if (n1x_boot_read(endpoint, 8, &pending) ||
		n1x_boot_read(endpoint, 0xd2c, &owned) ||
		n1x_boot_read(endpoint, 0xd28, &saved))
		return IRQ_NONE;
	if (!(pending & (owned & saved)) && !(pending & 0xc0))
		return IRQ_NONE;
	n1x_boot_write(endpoint, 0xd24, saved & owned);
	atomic_inc(&boot_irq_count);
	wake_up_all(&boot_waitq);
	return IRQ_HANDLED;
}

static void n1x_boot_delay(void *context, u32 interval_us)
{
	if (opt_enable_irq && boot_irq_registered && context == boot_dla &&
		interval_us >= 1000) {
		int before = atomic_read(&boot_irq_count);
		u32 owned = 0, saved = 0;

		if (!n1x_boot_read(boot_dla, 0xd2c, &owned) &&
			!n1x_boot_read(boot_dla, 0xd28, &saved))
			n1x_boot_write_irq_enable(boot_dla, saved | (0xc0U & owned));
		wait_event_timeout(boot_waitq,
					atomic_read(&boot_irq_count) != before,
					max(1UL, usecs_to_jiffies(interval_us)));
		return;
	}
	usleep_range(interval_us, interval_us + 100);
}

static int n1x_boot_board_policy(const struct firmware *firmware);

static int n1x_boot_live_board_policy(void)
{
	struct firmware carveout = {.size = N1X_VBIOS_CARVEOUT_SIZE};
	void __iomem *locator_io;
	resource_size_t rom_phys;
	u32 locator;
	void *rom;
	int err;

	locator_io = ioremap(N1X_DISP_VBIOS_LOCATOR_PHYS, sizeof(u32));
	if (!locator_io)
		return -ENOMEM;
	locator = readl(locator_io);
	iounmap(locator_io);
	rom_phys = (((u64)locator << 12) + N1X_VBIOS_CARVEOUT_BASE) & 0xffffffffULL;
	if (!rom_phys ||
		region_intersects(rom_phys, carveout.size, IORESOURCE_SYSTEM_RAM,
					IORES_DESC_NONE) != REGION_DISJOINT)
		return -EOPNOTSUPP;
	rom = memremap(rom_phys, carveout.size, MEMREMAP_WB);
	if (!rom)
		return -ENOMEM;
	carveout.data = rom;
	err = n1x_boot_board_policy(&carveout);
	memunmap(rom);
	return err;
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
 * not redistributed vendor firmware.
 */
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

	if (!firmware || !firmware->data || !firmware->size ||
		firmware->size > 0x110000 ||
		(strict_platform && firmware->size != 0x110000))
		return -EBADMSG;
	if (strict_platform) {
		sha256(firmware->data, firmware->size, digest);
		if (memcmp(digest, expected, sizeof(digest)))
			return -EBADMSG;
	}
	err = n1x_dla_parse_vbios_policy(firmware->data, firmware->size,
			&boot_hfrp.policy, &boot_hfrp.location);
	if (err)
		return err;
	err = n1x_dla_verify_vbios_pcir(firmware->data, firmware->size,
			&boot_hfrp.location);
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
	const struct firmware *firmware = NULL;
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
	if (!strict_platform) {
		err = n1x_boot_live_board_policy();
		if (err) {
			err = request_firmware_direct(&firmware, N1X_BOARD_FIRMWARE, boot_dla->dev);
			if (!err) {
				err = n1x_boot_board_policy(firmware);
				release_firmware(firmware);
			}
		}
	} else {
		err = request_firmware_direct(&firmware, N1X_BOARD_FIRMWARE, boot_dla->dev);
		if (!err) {
			err = n1x_boot_board_policy(firmware);
			release_firmware(firmware);
		}
	}
	if (err)
		return err;
	err = pm_runtime_resume_and_get(boot_dla->dev);
	if (err < 0)
		return err;
	err = n1x_boot_hfrp_read(boot_dla, 0, &stamp);
	if (!err && (strict_platform ? stamp != 0x20251722 : stamp < 0x20250425))
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
	boot_hfrp_held = true;
	boot_hfrp.phase = 1;
	err = n1x_hfrp_handshake(&io, &boot_hfrp.channel, 5001, 1000, &boot_hfrp.exchange);
	if (err)
		goto done;
	boot_hfrp.phase = 2;
	err = n1x_hfrp_discover_version(&io, &boot_hfrp.channel, 5001, 1000,
			&boot_hfrp.version, &boot_hfrp.exchange);
	if (!err && (boot_hfrp.version.major != 1 || boot_hfrp.version.minor < 5))
		err = -EOPNOTSUPP;
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
		WRITE_ONCE(boot_hfrp.powered, true);
		boot_hfrp.phase = 5;
	}
done:
	dev_info(boot_dla->dev,
		"HFRP phase=%u error=%d version=%u.%u command=%u completed=%u setup_reply=%#x/%u final_reply=%#x/%u power_accepted=%u; retained until checked shutdown\n",
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
	/* Reference acquired BEFORE publication. Only checked explicit shutdown
	 * may release it. Timeout/rejection/idle failure stays pinned; no guessed
	 * DMA quiescence or forced unload.
	 */
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto free;
	}
	sroot_io = (struct n1x_dla_io){boot_sroot, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dla_io = (struct n1x_dla_io){boot_dla, NULL, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	boot_attempted = true;
	boot_upload_held = true;
	err = n1x_dla_sroot_upload(&sroot_io, &dla_io, 16, words, 5001, 1000, &boot_result);
	dev_info(boot_dla->dev,
		"upload variant %u phase returned %d: reply_valid=%u raw=%#x idle=%u; DMA %pad retained until checked shutdown\n",
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
		maximum - boot_ping.dma < 4095) {
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
		maximum - boot_config.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_config.buffer, 0, 4096);
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	dma_wmb();
	/* Retain conservatively even on a stale-response/pre-send error. The
	 * preceding upload already pins module/runtime references until checked shutdown.
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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
		maximum - boot_config.dma < 4095)
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_apply_config_once();
	boot_config_apply.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(apply_config);

/* Windows setup preserves the actual board's provider +0xf4, substituting 4
 * only for zero. BIOS-300 supplies 1. Do not infer physical time units here.
 */
static int n1x_boot_apply_lpwr_once(void)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!enable_lpwr_config || !n1x_boot_machine())
		return -EPERM;
	if (!boot_sroot || !boot_dla || !boot_ping.verified ||
		!boot_region5.registered || !boot_config_apply.accepted ||
		!boot_hfrp.powered || !boot_hfrp.policy.boot_tables_valid)
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
		maximum - boot_lpwr.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot_lpwr.buffer, 0, 4096);
	err = n1x_dla_build_command23(boot_lpwr.buffer, 4096,
			boot_hfrp.gating.hal_flags[0], 1,
			boot_hfrp.policy.gating_bytes[0] ?: 4);
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_apply_lpwr_once();
	boot_lpwr.error = err;
	mutex_unlock(&boot_lock);
	return err ? err : count;
}
static DEVICE_ATTR_WO(apply_lpwr);

/* The physical P16 sends exactly {word=12,data=0} after idle. The original
 * Windows handler ACKs this notification without marking task completion.
 * Keep the qualification device powered under the existing runtime reference;
 * do not infer or execute a power-down policy from this notification.
 * Firmware holds a mailbox until ACK, and boot_lock excludes another reader.
 */
static int n1x_boot_drain_task_idle(struct n1x_dla_job *task)
{
	struct n1x_dla_io io = {boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	struct n1x_dla_response response;
	u32 word, data, i;
	int err;

	if (boot_queue.poisoned || task->idle_blocked)
		return -ENOTRECOVERABLE;
	if (boot_queue.active)
		return -EBUSY;
	for (i = 0; i < 16; i++) {
		err = n1x_boot_read(boot_dla, 8, &task->pending);
		if (err)
			return err;
		if (!(task->pending & 0xc0))
			return 0;
		if (task->pending & 0x40)
			return -EBUSY;
		err = n1x_boot_read(boot_dla, 0x40, &word);
		if (!err)
			err = n1x_boot_read(boot_dla, 0x44, &data);
		if (err)
			return err;
		if (word != 12 || data != 0)
			return -EBUSY; /* Unknown/error/stale response remains unacknowledged. */
		task->idle_blocked = true; /* An uncertain ACK cannot be replayed. */
		err = n1x_dla_take_response(&io, &response);
		if (err) {
			boot_queue.poisoned = true;
			return err;
		}
		if (response.word != 12 || response.data != 0) {
			boot_queue.poisoned = true;
			return -EPROTO;
		}
		task->idle_notifications++;
		task->idle_blocked = false;
	}
	task->idle_blocked = true;
	boot_queue.poisoned = true;
	return -ELOOP;
}

/* One owned diagnostic descriptor, not an arbitrary model/task UAPI.
 * A changed fence alone is insufficient; require submission acceptance and
 * SWGEN0, processing pending mailbox errors first. Never infer DMA quiescence.
 */
static int n1x_boot_publish_job(void *context, u32 value)
{
	struct n1x_dla_job *task = context;
	u8 *p = task->buffer;
	/* Both owned bootstrap descriptors were built by the fenced-v2 serializer:
	 * WRITE_SEM begins at 64, its value is at 73. No user descriptor is patched.
	 */
	if (p[64] != 0x80 || p[60] != 64 || p[62] != 14)
		return -EINVAL;
	p[73] = value;
	p[74] = value >> 8;
	p[75] = value >> 16;
	p[76] = value >> 24;
	WRITE_ONCE(*(__le32 *)(task->buffer + 768), cpu_to_le32(~value));
	dma_wmb();
	return 0;
}

static int n1x_boot_read_job_fence(void *context, u32 *value)
{
	struct n1x_dla_job *task = context;

	*value = le32_to_cpu(READ_ONCE(*(__le32 *)(task->buffer + 768)));
	/* Acquire result reads after the device's completion word. */
	dma_rmb();
	return 0;
}

static int n1x_boot_run_task(struct n1x_dla_job *task)
{
	struct n1x_dla_io io = {boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	struct n1x_dla_job_ops ops = {task, n1x_boot_publish_job, n1x_boot_read_job_fence};

	return n1x_dla_job_execute(&boot_queue, &io, &ops, task);
}

static int n1x_boot_bypass_task_once(void)
{
	struct n1x_dla_task_args args = {0};
	u64 maximum = DMA_BIT_MASK(36), network;
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
	if (boot_queue.last_fence == U32_MAX)
		return -EOVERFLOW;
	err = n1x_boot_drain_task_idle(&boot_task);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot_dla->dev, maximum);
	if (err)
		return err;
	boot_task.buffer = dma_alloc_coherent(boot_dla->dev, 4096,
						&boot_task.dma, GFP_KERNEL);
	if (!boot_task.buffer)
		return -ENOMEM;
	if (!boot_task.dma || (boot_task.dma & 255) || boot_task.dma > maximum ||
		maximum - boot_task.dma < 4095) {
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
	err = n1x_boot_run_task(&boot_task);
	if (err && !boot_task.attempted)
		goto free;
	return err;
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
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

/* Explicit diagnostic only. Successful preparation already pins runtime PM
 * and module lifetime. Serialize with startup, inference and other queries.
 * Reading hfrp_status never sends a command or consumes a mailbox response.
 */
static ssize_t query_power_state_store(struct device *dev, struct device_attribute *attr,
						const char *buf, size_t count)
{
	struct n1x_dla_io io;
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "query"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!enable_hfrp || !n1x_boot_machine())
		err = -EPERM;
	else if (!boot_dla || boot_dla->dev != dev || !boot_dla->hfrp || !boot_sroot)
		err = -ENODEV;
	else if (!boot_hfrp.attempted || !boot_hfrp.powered || boot_hfrp.error)
		err = -EHOSTDOWN;
	else if (boot_hfrp.channel.state != N1X_HFRP_READY)
		err = boot_hfrp.channel.state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EBUSY;
	else {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_hfrp_write,
						n1x_boot_hfrp_read, n1x_boot_delay};
		boot_hfrp.state_attempted = true;
		memset(&boot_hfrp.power_state, 0, sizeof(boot_hfrp.power_state));
		memset(&boot_hfrp.state_exchange, 0, sizeof(boot_hfrp.state_exchange));
		err = n1x_hfrp_query_power_state(&io, &boot_hfrp.channel, &boot_hfrp.version,
				5001, 1000, &boot_hfrp.power_state, &boot_hfrp.state_exchange);
		boot_hfrp.state_error = err;
		dev_info(dev, "HFRP state query error=%d valid=%u raw=%#x reply=%#x length=%u channel=%u; no idle guarantee\n",
			err, boot_hfrp.power_state.valid, boot_hfrp.power_state.raw,
			boot_hfrp.state_exchange.reply.header, boot_hfrp.state_exchange.reply.length,
			boot_hfrp.channel.state);
	}
	mutex_unlock(&boot_lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(query_power_state);

static ssize_t hfrp_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev) {
		mutex_unlock(&boot_lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
		"enabled=%u attempted=%u phase=%u error=%d state=%u pending=%u stamp=%#x version=%u.%u configured=%u command=%u completed=%u setup_reply=%#x setup_length=%u setup_payload=%*phN final_reply=%#x final_length=%u final_payload=%*phN power_accepted=%u frequency_khz=%u gating=%#x query_attempted=%u query_error=%d query_valid=%u query_raw=%#x query_reply=%#x query_length=%u query_payload=%*phN\n",
		enable_hfrp, boot_hfrp.attempted, boot_hfrp.phase, boot_hfrp.error,
		boot_hfrp.channel.state, boot_hfrp.channel.pending_sequence,
		boot_hfrp.version.stamp, boot_hfrp.version.major, boot_hfrp.version.minor,
		boot_hfrp.setup.configured, boot_hfrp.setup.command, boot_hfrp.setup.completed_commands,
		boot_hfrp.setup.exchange.reply.header, boot_hfrp.setup.exchange.reply.length,
		(int)boot_hfrp.setup.exchange.reply.length, boot_hfrp.setup.exchange.reply.payload,
		boot_hfrp.exchange.reply.header, boot_hfrp.exchange.reply.length,
		(int)boot_hfrp.exchange.reply.length, boot_hfrp.exchange.reply.payload,
		boot_hfrp.powered, boot_hfrp.policy.frequency_khz, boot_hfrp.gating.request_mask,
		boot_hfrp.state_attempted, boot_hfrp.state_error, boot_hfrp.power_state.valid,
		boot_hfrp.power_state.raw, boot_hfrp.state_exchange.reply.header,
		boot_hfrp.state_exchange.reply.length, (int)boot_hfrp.state_exchange.reply.length,
		boot_hfrp.state_exchange.reply.payload);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_hfrp_status = __ATTR(hfrp_status, 0400, hfrp_status_show, NULL);

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev) {
		mutex_unlock(&boot_lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
		"dla_bound=%u sroot_bound=%u enabled=%u attempted=%u error=%d reply_valid=%u reply=%#x idle_observed=%u dma_retained=%u ping_enabled=%u ping_attempted=%u ping_error=%d ping_verified=%u ping_word=%#x ping_retained=%u config_enabled=%u config_attempted=%u config_error=%d config_valid=%u config_reply_valid=%u config_reply=%#x config_retained=%u region5_enabled=%u region5_attempted=%u region5_error=%d region5_registered=%u region5_retained=%u config_apply_enabled=%u config_apply_attempted=%u config_apply_error=%d config_apply_accepted=%u config_apply_reply_valid=%u config_apply_reply=%#x lpwr_enabled=%u lpwr_attempted=%u lpwr_error=%d lpwr_accepted=%u lpwr_reply_valid=%u lpwr_reply=%#x lpwr_retained=%u task_enabled=%u task_attempted=%u task_error=%d task_response_valid=%u task_reply=%#x task_data=%#x task_accepted=%u task_signal_seen=%u task_fence=%#x task_pending=%#x task_verified=%u task_retained=%u task_idle_notifications=%u task_idle_blocked=%u task_fence_value=%#x queue_poisoned=%u irq_enabled=%u irq_registered=%u irq_count=%u preempt_enabled=%u preempt_attempted=%u preempt_error=%d preempt_accepted=%u preempt_reply_valid=%u preempt_reply=%#x\n",
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
		boot_task.fence, boot_task.pending, boot_task.verified, !!boot_task.buffer,
		boot_task.idle_notifications, boot_task.idle_blocked,
		boot_task.fence_value, boot_queue.poisoned,
		opt_enable_irq, boot_irq_registered, atomic_read(&boot_irq_count),
		enable_preempt, boot_preempt.attempted, boot_preempt.error,
		boot_preempt.accepted, boot_preempt.transaction.response_valid,
		boot_preempt.transaction.response.word);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_status = __ATTR(status, 0400, status_show, NULL);

/* accel->lock (if present) -> boot_lock, matching submission. Stop is terminal
 * for this module instance. A fresh load constructs a fresh channel/queue.
 * No speculative reset or rollback is attempted after any stop failure.
 */
static int n1x_boot_stop_hardware(struct device *dev)
{
	struct n1x_dla_io io;
	int err = 0;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev || !boot_sroot) {
		err = -ENODEV;
		goto out;
	}
	if (boot_shutdown.attempted) {
		err = boot_shutdown.error;
		goto out;
	}
	WRITE_ONCE(boot_stopping, true);
	boot_shutdown.attempted = true;
	boot_shutdown.stage = "preflight";
	if (!boot_hfrp.attempted && !boot_attempted) {
		boot_shutdown.hardware_off = true; /* No write/DMA publisher existed. */
		goto done;
	}
	/* Only the healthy, retired protocol has a recovered shutdown contract.
	 * In particular a poisoned queue, failed upload or incomplete HFRP setup
	 * must keep ALL mappings, PM and module references. No forced recovery.
	 */
	if (!n1x_boot_machine() || !boot_hfrp.powered || boot_hfrp.error ||
		(boot_hfrp.gating.request_mask & 0x70) != 0x70 ||
		boot_queue.poisoned || boot_queue.active || boot_queue.owner_job ||
		(boot_attempted && (boot_error || !boot_result.reply_valid ||
		!boot_result.idle_observed || boot_result.reply.status || boot_result.reply.detail)) ||
		(boot_ping.attempted && !boot_ping.verified) ||
		(boot_config.attempted && !boot_config.valid) ||
		(boot_region5.attempted && !boot_region5.registered) ||
		(boot_config_apply.attempted && !boot_config_apply.accepted) ||
		(boot_lpwr.attempted && !boot_lpwr.accepted) ||
		(boot_task.attempted && !boot_task.verified)) {
		err = -ENOTRECOVERABLE;
		goto done;
	}
	boot_shutdown.stage = "dma-domain";
	err = n1x_boot_dma_gate(boot_dla);
	if (!err)
		err = n1x_boot_dma_gate(boot_sroot);
	if (err)
		goto done;
	boot_shutdown.stage = "idle-drain";
	if (boot_task.attempted) {
		err = n1x_boot_drain_task_idle(&boot_task);
		if (err)
			goto done;
	}
	/* Follow the recovered Windows mask operation and flush the write before
	 * power-off. Do NOT require every raw route/mask intersection bit to read
	 * back zero: the physical P16 retains bit 24 in both registers after this
	 * operation.
	 */
	boot_shutdown.stage = "irq-mask";
	err = n1x_boot_read(boot_dla, 0xd2c, &boot_shutdown.irq_owned);
	if (!err)
		err = n1x_boot_read(boot_dla, 0xd28, &boot_shutdown.irq_mask_before);
	if (!err)
		err = n1x_boot_write(boot_dla, 0xd24,
			boot_shutdown.irq_mask_before & boot_shutdown.irq_owned);
	if (!err)
		err = n1x_boot_read(boot_dla, 0xd28, &boot_shutdown.irq_mask_after);
	if (err)
		goto done;
	boot_shutdown.irq_readback_valid = true;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_hfrp_write,
					n1x_boot_hfrp_read, n1x_boot_delay};
	boot_shutdown.stage = "hfrp-stop";
	err = n1x_hfrp_stop(&io, &boot_hfrp.channel, &boot_hfrp.version,
				5001, 1000, &boot_shutdown.transport);
	if (err)
		goto done;
	if (!boot_shutdown.transport.confirmed) {
		err = -EIO;
		goto done;
	}
	boot_shutdown.hardware_off = true;
	WRITE_ONCE(boot_hfrp.powered, false);
	boot_shutdown.stage = "dma-release";
	/* SROOT's successful synchronous upload transaction was already consumed
	 * and firmware ran from its internal copy. Its failed/uncertain path is
	 * excluded above. DLA's continuing region/task DMA is now power gated.
	 */
#define FREE_DLA_BUFFER(object, bytes) do { \
	if ((object).buffer) { \
		dma_free_coherent(boot_dla->dev, bytes, (object).buffer, (object).dma); \
		(object).buffer = NULL; (object).dma = 0; \
	} \
} while (0)
	FREE_DLA_BUFFER(boot_task, 4096);
	FREE_DLA_BUFFER(boot_lpwr, 4096);
	FREE_DLA_BUFFER(boot_region5, N1X_REGION5_ALLOC);
	FREE_DLA_BUFFER(boot_config, 4096);
	FREE_DLA_BUFFER(boot_ping, 4096);
#undef FREE_DLA_BUFFER
	if (boot_buffer) {
		dma_free_coherent(boot_sroot->dev, N1X_BOOT_BYTES, boot_buffer, boot_dma);
		boot_buffer = NULL;
		boot_dma = 0;
	}
done:
	boot_shutdown.error = err;
	dev_info(dev, "shutdown stage=%s error=%d power_reply=%u state_valid=%u state=%#x off=%u irq_owned=%#x irq_before=%#x irq_after=%#x irq_readback=%u; %s\n",
		boot_shutdown.stage, err, boot_shutdown.transport.accepted, boot_shutdown.transport.state.valid,
		boot_shutdown.transport.state.raw, boot_shutdown.hardware_off,
		boot_shutdown.irq_owned, boot_shutdown.irq_mask_before,
		boot_shutdown.irq_mask_after, boot_shutdown.irq_readback_valid,
		err ? "DMA/lifetime retained, no retry" : "hardware stopped");
out:
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_stop_device(struct device *dev)
{
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	int err;

	if (!endpoint || endpoint->sroot)
		return -ENODEV;
	/* Keep the same lock order for shutdown and EXEC; never call this while
	 * holding boot_lock. accel_stop frees its list only after hardware success.
	 */
	err = endpoint->accel ? n1x_dla_accel_stop(endpoint->accel, n1x_boot_stop_hardware) :
					n1x_boot_stop_hardware(dev);
	if (err)
		return err;
	mutex_lock(&boot_lock);
	if (!boot_shutdown.released) {
		boot_shutdown.released = true;
		boot_shutdown.stage = "released";
		if (boot_upload_held) {
			pm_runtime_put_noidle(boot_sroot->dev);
			pm_runtime_put_noidle(boot_dla->dev);
			boot_upload_held = false;
			if (!boot_unpinned)
				module_put(THIS_MODULE);
		}
		if (boot_hfrp_held) {
			pm_runtime_put_noidle(boot_dla->dev);
			boot_hfrp_held = false;
			if (!boot_unpinned)
				module_put(THIS_MODULE);
		}
		boot_unpinned = false;
	}
	mutex_unlock(&boot_lock);
	return 0;
}

static ssize_t shutdown_store(struct device *dev, struct device_attribute *attr,
					const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "stop"))
		return -EPERM;
	err = n1x_boot_stop_device(dev);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(shutdown);

static ssize_t shutdown_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	ssize_t size;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev) {
		mutex_unlock(&boot_lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
		"attempted=%u stage=%s error=%d accepted=%u state_valid=%u state=%#x hardware_off=%u released=%u hfrp_held=%u upload_held=%u irq_owned=%#x irq_before=%#x irq_after=%#x irq_readback=%u power_attempted=%u power_head_attempted=%u power_reply_valid=%u power_reply=%#x power_length=%u power_payload=%*phN query_head_attempted=%u query_reply_valid=%u query_reply=%#x query_length=%u query_payload=%*phN channel=%u pending=%u\n",
		boot_shutdown.attempted, boot_shutdown.stage ?: "not-started", boot_shutdown.error,
		boot_shutdown.transport.accepted, boot_shutdown.transport.state.valid, boot_shutdown.transport.state.raw,
		boot_shutdown.hardware_off, boot_shutdown.released, boot_hfrp_held, boot_upload_held,
		boot_shutdown.irq_owned, boot_shutdown.irq_mask_before,
		boot_shutdown.irq_mask_after, boot_shutdown.irq_readback_valid,
		boot_shutdown.transport.attempted, boot_shutdown.transport.power.tx.head_publish_attempted,
		boot_shutdown.transport.power.reply_valid, boot_shutdown.transport.power.reply.header,
		boot_shutdown.transport.power.reply.length, (int)boot_shutdown.transport.power.reply.length,
		boot_shutdown.transport.power.reply.payload,
		boot_shutdown.transport.query.tx.head_publish_attempted, boot_shutdown.transport.query.reply_valid,
		boot_shutdown.transport.query.reply.header, boot_shutdown.transport.query.reply.length,
		(int)boot_shutdown.transport.query.reply.length, boot_shutdown.transport.query.reply.payload,
		boot_hfrp.channel.state, boot_hfrp.channel.pending_sequence);
	mutex_unlock(&boot_lock);
	return size;
}
static struct device_attribute dev_attr_shutdown_status =
	__ATTR(shutdown_status, 0400, shutdown_status_show, NULL);

static int n1x_boot_start_once(void)
{
	int err;

	if (!n1x_boot_machine() || !enable_hfrp || !enable_upload ||
		!enable_ping || !enable_region5 || !enable_config_snapshot ||
		!enable_config_apply || !enable_lpwr_config || !enable_bypass_task)
		return -EPERM;
	if (!boot_dla || !boot_dla->hfrp || !boot_sroot)
		return -ENODEV;
	if (boot_hfrp.attempted || boot_attempted || boot_ping.attempted ||
		boot_region5.attempted || boot_config.attempted ||
		boot_config_apply.attempted || boot_lpwr.attempted ||
		boot_task.attempted)
		return -EALREADY;
	err = n1x_boot_prepare_hfrp_once();
	boot_hfrp.error = err;
	if (err)
		return err;
	err = n1x_boot_upload();
	boot_error = err;
	if (err)
		return err;
	err = n1x_boot_ping_once();
	boot_ping.error = err;
	if (err)
		return err;
	err = n1x_boot_init_region5_once();
	boot_region5.error = err;
	if (err)
		return err;
	err = n1x_boot_read_config_once();
	boot_config.error = err;
	if (err)
		return err;
	err = n1x_boot_apply_config_once();
	boot_config_apply.error = err;
	if (err)
		return err;
	err = n1x_boot_apply_lpwr_once();
	boot_lpwr.error = err;
	if (err)
		return err;
	err = n1x_boot_bypass_task_once();
	boot_task.error = err;
	return err;
}

static ssize_t start_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "start"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_boot_start_once();
	mutex_unlock(&boot_lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(start);

static int n1x_boot_enable_preempt_once(void);

static ssize_t preempt_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "enable"))
		return -EPERM;
	mutex_lock(&boot_lock);
	if (boot_stopping)
		err = -ESHUTDOWN;
	else if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else if (boot_preempt.attempted)
		err = -EALREADY;
	else
		err = n1x_boot_enable_preempt_once();
	mutex_unlock(&boot_lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(preempt);

static struct attribute *boot_attrs[] = {&dev_attr_start.attr, &dev_attr_upload.attr, &dev_attr_ping.attr, &dev_attr_read_config.attr,
	&dev_attr_shutdown.attr, &dev_attr_shutdown_status.attr,
	&dev_attr_prepare_hfrp.attr, &dev_attr_hfrp_status.attr, &dev_attr_query_power_state.attr,
	&dev_attr_config_snapshot.attr, &dev_attr_init_region5.attr,
	&dev_attr_apply_config.attr, &dev_attr_apply_lpwr.attr, &dev_attr_bypass_task.attr,
	&dev_attr_preempt.attr, &dev_attr_status.attr, NULL};

static umode_t boot_attr_is_visible(struct kobject *kobj, struct attribute *attr, int n)
{
	struct device *dev = kobj_to_dev(kobj);
	const struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);

	(void)n;
	if (!endpoint || endpoint->sroot)
		return 0;
	if (!debug_sysfs && autostart && attr != &dev_attr_status.attr)
		return 0;
	return attr->mode;
}

static const struct attribute_group boot_group = {
	.attrs = boot_attrs,
	.is_visible = boot_attr_is_visible,
};
__ATTRIBUTE_GROUPS(boot);

/* Enable HAL-1 preemption (method 0x31c, data=1 -> 0x1c02) while idle. */
static int n1x_boot_enable_preempt_once(void)
{
	struct n1x_dla_io io;
	u32 pending, attempt;
	int err;

	if (!enable_preempt || !n1x_boot_machine())
		return -EPERM;
	if (!boot_dla || !boot_sroot || !boot_task.verified || !boot_lpwr.accepted ||
		!boot_hfrp.powered || !boot_hfrp.policy.boot_tables_valid)
		return -ENODEV;
	if (boot_preempt.attempted)
		return boot_preempt.accepted ? 0 : (boot_preempt.error ?: -EIO);
	err = n1x_boot_drain_task_idle(&boot_task);
	if (err)
		return err;
	err = n1x_boot_read(boot_dla, 8, &pending);
	if (err)
		return err;
	if (pending & 0xc0)
		return -EBUSY;
	io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
	boot_preempt.attempted = true;
	boot_preempt.transaction.method_attempted = 1;
	err = n1x_dla_send_method(&io, 0x31c, 1, 5001, 1000);
	if (err)
		goto out;
	for (attempt = 0; attempt < 5001; attempt++) {
		err = n1x_dla_take_response(&io, &boot_preempt.transaction.response);
		if (err != -EAGAIN)
			break;
		if (attempt + 1 < 5001)
			n1x_boot_delay(boot_dla, 1000);
	}
	if (err == -EAGAIN)
		err = -ETIMEDOUT;
	if (err)
		goto out;
	boot_preempt.transaction.response_valid = 1;
	if (boot_preempt.transaction.response.word != 0x1c02 ||
		boot_preempt.transaction.response.data != 1) {
		err = -EPROTO;
		goto out;
	}
	boot_preempt.accepted = true;
out:
	boot_preempt.error = err;
	if (err)
		boot_queue.poisoned = true;
	return err;
}

static int n1x_boot_accel_ready(struct device *dev)
{
	if (boot_stopping || !enable_accel || !enable_preempt ||
		!n1x_boot_machine() || !boot_dla || boot_dla->dev != dev ||
		!boot_sroot || !boot_task.verified || !boot_lpwr.accepted ||
		!boot_hfrp.powered || !boot_hfrp.policy.boot_tables_valid)
		return -ENODEV;
	return n1x_boot_dma_gate(boot_dla);
}

static int n1x_boot_accel_start(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_job_ops *ops)
{
	struct n1x_dla_io io;
	int err;

	mutex_lock(&boot_lock);
	err = n1x_boot_accel_ready(dev);
	if (!err && !boot_preempt.accepted)
		err = n1x_boot_enable_preempt_once();
	if (!err)
		err = n1x_boot_drain_task_idle(job);
	if (!err) {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
		err = n1x_dla_job_start(&boot_queue, &io, ops, job);
	}
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_accel_poll(struct device *dev, struct n1x_dla_job *job,
					const struct n1x_dla_job_ops *ops)
{
	struct n1x_dla_io io;
	int err;

	mutex_lock(&boot_lock);
	err = n1x_boot_accel_ready(dev);
	if (!err) {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
		err = n1x_dla_job_poll(&boot_queue, &io, ops, job);
	}
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_accel_preempt(struct device *dev, struct n1x_dla_job *job,
					const struct n1x_dla_preempt_ops *ops,
					u64 request_dma, u32 sequence,
					struct n1x_dla_preempt_result *result)
{
	struct n1x_dla_io io;
	int err;

	mutex_lock(&boot_lock);
	err = n1x_boot_accel_ready(dev);
	if (!err) {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
		err = n1x_dla_job_preempt(&boot_queue, &io, ops, job, request_dma, sequence, result);
	}
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_accel_resume(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_preempt_ops *ops)
{
	struct n1x_dla_io io;
	int err;

	mutex_lock(&boot_lock);
	err = n1x_boot_accel_ready(dev);
	if (!err) {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
		err = n1x_dla_job_resume(&boot_queue, &io, ops, job);
	}
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_accel_fail(struct device *dev, struct n1x_dla_job *job, int error)
{
	int err;

	mutex_lock(&boot_lock);
	if (!boot_dla || boot_dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_dla_job_fail(&boot_queue, job, error);
	mutex_unlock(&boot_lock);
	return err;
}

static const struct n1x_dla_accel_control_ops n1x_boot_control_ops = {
	.start = n1x_boot_accel_start,
	.poll = n1x_boot_accel_poll,
	.preempt = n1x_boot_accel_preempt,
	.resume = n1x_boot_accel_resume,
	.fail = n1x_boot_accel_fail,
};

static const struct component_ops n1x_sroot_component_ops;
static const struct component_master_ops n1x_master_ops;
static int n1x_compare_sroot(struct device *dev, void *data);

/* accel->lock is outside boot_lock; sysfs never takes accel->lock. */
static int n1x_boot_accel_submit(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_job_ops *ops)
{
	struct n1x_dla_io io;
	int err;

	mutex_lock(&boot_lock);
	if (boot_stopping || !enable_accel || !n1x_boot_machine() || !boot_dla || boot_dla->dev != dev ||
		!boot_sroot || !boot_task.verified || !boot_lpwr.accepted ||
		!boot_hfrp.powered || !boot_hfrp.policy.boot_tables_valid) {
		err = -ENODEV;
		goto out;
	}
	/* Successful startup already retained runtime references. */
	err = n1x_boot_dma_gate(boot_dla);
	if (!err)
		err = n1x_boot_drain_task_idle(job);
	if (!err) {
		io = (struct n1x_dla_io){boot_dla, n1x_boot_write, n1x_boot_read, n1x_boot_delay};
		err = n1x_dla_job_execute(&boot_queue, &io, ops, job);
	}
out:
	mutex_unlock(&boot_lock);
	return err;
}

static int n1x_boot_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct n1x_boot_endpoint *endpoint;
	struct resource *first, *second;
	struct resource registers = {.flags = IORESOURCE_MEM};
	unsigned long long revision;
	bool sroot, irq_registered = false;
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
	if (!first || !second ||
		resource_size(first) < (sroot ? 12 * 16 + 4 : 0x40000) ||
		resource_size(second) < (sroot ? 0x10 : 0x200) ||
		(strict_platform && (platform_get_resource(pdev, IORESOURCE_MEM, 2) ||
		first->start != (sroot ? 0x18010000 : 0x19000000) ||
		resource_size(first) != (sroot ? 0x1000 : 0x40000) ||
		second->start != (sroot ? 0x18010000 : 0x31380000) ||
		resource_size(second) != (sroot ? 0x10 : 0x10000))))
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
		if (opt_enable_irq) {
			int irq = platform_get_irq_optional(pdev, 0);

			if (irq > 0) {
				int irq_err = devm_request_irq(dev, irq, n1x_boot_irq_handler,
									IRQF_SHARED, dev_name(dev), endpoint);

				if (irq_err)
					dev_warn(dev, "IRQ %d request failed (%d); falling back to polling\n",
						irq, irq_err);
				else
					irq_registered = true;
			} else {
				dev_warn(dev, "no ACPI IRQ resource (%d); falling back to polling\n",
					irq);
			}
		}
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
		if (sroot) {
			boot_sroot = endpoint;
		} else {
			boot_dla = endpoint;
			boot_irq_registered = irq_registered;
		}
		platform_set_drvdata(pdev, endpoint);
	}
	mutex_unlock(&boot_lock);
	if (err)
		return err;
	if (sroot) {
		err = component_add(dev, &n1x_sroot_component_ops);
	} else {
		struct component_match *match = NULL;

		component_match_add(dev, &match, n1x_compare_sroot, NULL);
		err = component_master_add_with_match(dev, &n1x_master_ops, match);
	}
	if (err) {
		mutex_lock(&boot_lock);
		if (sroot) {
			boot_sroot = NULL;
		} else {
			boot_dla = NULL;
			boot_irq_registered = false;
		}
		mutex_unlock(&boot_lock);
	}
	return err;
}

static bool boot_suspended;

static void n1x_boot_reset_state(void)
{
	if (!boot_shutdown.hardware_off || !boot_shutdown.released ||
		boot_buffer || boot_ping.buffer || boot_config.buffer ||
		boot_region5.buffer || boot_lpwr.buffer || boot_task.buffer)
		return;
	memset(&boot_result, 0, sizeof(boot_result));
	boot_error = 0;
	boot_attempted = false;
	WRITE_ONCE(boot_stopping, false);
	boot_hfrp_held = false;
	boot_upload_held = false;
	boot_unpinned = false;
	memset(&boot_shutdown, 0, sizeof(boot_shutdown));
	memset(&boot_hfrp, 0, sizeof(boot_hfrp));
	memset(&boot_ping, 0, sizeof(boot_ping));
	memset(&boot_config, 0, sizeof(boot_config));
	memset(&boot_region5, 0, sizeof(boot_region5));
	memset(&boot_config_apply, 0, sizeof(boot_config_apply));
	memset(&boot_lpwr, 0, sizeof(boot_lpwr));
	memset(&boot_task, 0, sizeof(boot_task));
	memset(&boot_preempt, 0, sizeof(boot_preempt));
	memset(&boot_queue, 0, sizeof(boot_queue));
}

static int n1x_sroot_component_bind(struct device *comp, struct device *master,
				    void *data)
{
	(void)comp;
	(void)master;
	(void)data;
	return 0;
}

static void n1x_sroot_component_unbind(struct device *comp, struct device *master,
				       void *data)
{
	(void)comp;
	(void)master;
	(void)data;
}

static const struct component_ops n1x_sroot_component_ops = {
	.bind = n1x_sroot_component_bind,
	.unbind = n1x_sroot_component_unbind,
};

static int n1x_compare_sroot(struct device *dev, void *data)
{
	struct acpi_device *adev = ACPI_COMPANION(dev);

	(void)data;
	return adev && !strcmp(acpi_device_hid(adev), "NVDA8600");
}

static int n1x_master_bind(struct device *master)
{
	struct n1x_boot_endpoint *dla_for_accel = NULL;
	int err;

	err = component_bind_all(master, NULL);
	if (err)
		return err;
	mutex_lock(&boot_lock);
	if (boot_dla && boot_sroot) {
		int start_err = 0;

		if (autostart && !boot_hfrp.attempted) {
			start_err = n1x_boot_start_once();
			if (start_err) {
				dev_err(boot_dla->dev,
					"automatic startup failed (%d); device remains fail-closed\n",
					start_err);
			} else {
				if (boot_upload_held)
					module_put(THIS_MODULE);
				if (boot_hfrp_held)
					module_put(THIS_MODULE);
				boot_unpinned = true;
				dev_info(boot_dla->dev,
					"N1X NPU initialized in-kernel (HFRP v%u.%u, %u kHz)\n",
					boot_hfrp.version.major, boot_hfrp.version.minor,
					boot_hfrp.policy.frequency_khz);
			}
		}
		if (!start_err && enable_accel && !boot_dla->accel)
			dla_for_accel = boot_dla;
	}
	mutex_unlock(&boot_lock);
	if (dla_for_accel) {
		struct n1x_dla_accel *accel;

		accel = n1x_dla_accel_register(dla_for_accel->dev, n1x_boot_accel_submit);
		if (IS_ERR(accel)) {
			component_unbind_all(master, NULL);
			return PTR_ERR(accel);
		}
		if (enable_preempt)
			n1x_dla_accel_set_control(accel, &n1x_boot_control_ops);
		mutex_lock(&boot_lock);
		dla_for_accel->accel = accel;
		mutex_unlock(&boot_lock);
	}
	return 0;
}

static void n1x_master_unbind(struct device *master)
{
	struct n1x_dla_accel *accel = NULL;
	bool do_stop = false;

	mutex_lock(&boot_lock);
	if (boot_dla && boot_sroot &&
		(boot_hfrp.attempted || boot_attempted) &&
		!boot_shutdown.attempted)
		do_stop = true;
	mutex_unlock(&boot_lock);
	if (do_stop) {
		int err = n1x_boot_stop_device(master);

		if (err)
			dev_err(master,
				"unbind shutdown failed (%d); retaining DMA, no forced reset\n",
				err);
	}
	mutex_lock(&boot_lock);
	if (boot_dla && boot_dla->accel) {
		accel = boot_dla->accel;
		boot_dla->accel = NULL;
	}
	mutex_unlock(&boot_lock);
	if (accel)
		n1x_dla_accel_unregister(accel);
	mutex_lock(&boot_lock);
	if (boot_shutdown.released) {
		n1x_boot_reset_state();
		boot_suspended = false;
	}
	mutex_unlock(&boot_lock);
	component_unbind_all(master, NULL);
}

static const struct component_master_ops n1x_master_ops = {
	.bind = n1x_master_bind,
	.unbind = n1x_master_unbind,
};

static void n1x_boot_remove(struct platform_device *pdev)
{
	struct n1x_boot_endpoint *endpoint = platform_get_drvdata(pdev);

	if (endpoint && endpoint->sroot)
		component_del(&pdev->dev, &n1x_sroot_component_ops);
	else if (endpoint)
		component_master_del(&pdev->dev, &n1x_master_ops);
	mutex_lock(&boot_lock);
	if (boot_sroot && boot_sroot->dev == &pdev->dev)
		boot_sroot = NULL;
	if (boot_dla && boot_dla->dev == &pdev->dev) {
		boot_dla = NULL;
		boot_irq_registered = false;
	}
	mutex_unlock(&boot_lock);
}

static int n1x_boot_suspend(struct device *dev)
{
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	bool active_boot;
	int err = 0;

	if (!endpoint || endpoint->sroot)
		return 0;
	mutex_lock(&boot_lock);
	active_boot = (boot_attempted || boot_hfrp.attempted) && !boot_shutdown.released;
	if (active_boot && (!autostart || boot_queue.active ||
				boot_queue.owner_job || boot_queue.poisoned))
		err = -EBUSY;
	mutex_unlock(&boot_lock);
	if (err || !active_boot)
		return err;
	err = n1x_boot_stop_device(dev);
	if (err)
		return err;
	mutex_lock(&boot_lock);
	n1x_boot_reset_state();
	boot_suspended = true;
	mutex_unlock(&boot_lock);
	return 0;
}

static int n1x_boot_resume(struct device *dev)
{
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	int err = 0;

	if (!endpoint || endpoint->sroot)
		return 0;
	mutex_lock(&boot_lock);
	if (boot_suspended && boot_dla && boot_sroot) {
		boot_suspended = false;
		err = n1x_boot_start_once();
		if (err) {
			dev_err(dev,
				"resume startup failed (%d); device remains fail-closed\n",
				err);
		} else {
			if (boot_upload_held)
				module_put(THIS_MODULE);
			if (boot_hfrp_held)
				module_put(THIS_MODULE);
			boot_unpinned = true;
		}
	}
	mutex_unlock(&boot_lock);
	if (!err && endpoint->accel)
		n1x_dla_accel_resume(endpoint->accel);
	return err;
}

static int n1x_boot_runtime_suspend(struct device *dev)
{
	/* Runtime autosuspend cannot perform the explicit joint-endpoint teardown
	 * or silently restart this one-attempt module instance.
	 */
	return -EBUSY;
}

static void n1x_boot_system_shutdown(struct platform_device *pdev)
{
	struct n1x_boot_endpoint *endpoint = platform_get_drvdata(pdev);
	int err;
	/* DLA owns the joint lifetime of both endpoints. ROT0's callback must not
	 * stop its transport while DLA teardown still needs it.
	 */
	if (!endpoint || endpoint->sroot)
		return;
	err = n1x_boot_stop_device(&pdev->dev);
	if (err)
		dev_err(&pdev->dev, "shutdown failed (%d); retaining DMA, no forced reset\n", err);
}

static const struct dev_pm_ops n1x_boot_pm = {
	.runtime_suspend = n1x_boot_runtime_suspend,
	.suspend = n1x_boot_suspend,
	.resume = n1x_boot_resume,
	.freeze = n1x_boot_suspend,
	.thaw = n1x_boot_resume,
	.poweroff = n1x_boot_suspend,
	.restore = n1x_boot_resume,
};
static const struct acpi_device_id n1x_boot_ids[] = {{"NVDA200A", 0}, {"NVDA8600", 0}, {}};
MODULE_DEVICE_TABLE(acpi, n1x_boot_ids);
static struct platform_driver n1x_boot_driver = {
	.probe = n1x_boot_probe,
	.remove = n1x_boot_remove,
	.shutdown = n1x_boot_system_shutdown,
	.driver = {
		.name = "n1x-npu-bootstrap",
		.acpi_match_table = n1x_boot_ids,
		.dev_groups = boot_groups,
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
MODULE_DESCRIPTION("NVIDIA N1X (GB10/T264) NPU DRM compute accelerator driver");
MODULE_FIRMWARE(N1X_BOOT_FIRMWARE);
MODULE_FIRMWARE(N1X_BOOT_FALLBACK_FIRMWARE);
