// SPDX-License-Identifier: GPL-2.0-only
/* NVIDIA N1X (GB10/T264) NPU Linux kernel driver (NVDA200A DLA + NVDA8600 SROOT).
 * Performs in-kernel HFRP power/clock bring-up, SROOT firmware upload, region/LPWR
 * initialization, and exposes the /dev/accel/accel* DRM compute accelerator node.
 * Retains DMA allocations across uncertain hardware faults (fail-closed).
 */
#include <linux/acpi.h>
#include <linux/capability.h>
#include <linux/cleanup.h>
#include <linux/kref.h>
#include <linux/list.h>
#include <linux/component.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/dma-map-ops.h>
#include <linux/dma-mapping.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/interrupt.h>
#include <linux/iommu.h>
#include <linux/iommu-dma.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/property.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
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
#include "n1x_sroot_client.h"
#include "n1x_sroot_firmware.h"

#define N1X_BOOT_FIRMWARE "nvidia/n1x/sroot-v0.bin"
#define N1X_BOOT_FALLBACK_FIRMWARE "nvidia/n1x/sroot-v1.bin"
#define N1X_REGION5_ALLOC (1U << 20)
#define N1X_REGION5_OFFSET 16U
#define N1X_REGION5_STRIDE 52U
#define N1X_REGION5_BYTES (((N1X_REGION5_ALLOC - N1X_REGION5_OFFSET - 8U) / \
				N1X_REGION5_STRIDE) * N1X_REGION5_STRIDE + 8U)
/* GB10/T264 display register holding the 4 KiB-shifted VBIOS carveout.
 * NVDA3000 describes SVP registers, not this display aperture.
 */
#define N1X_DISP_VBIOS_LOCATOR_PHYS 0x1182bc80ULL
#define N1X_VBIOS_CARVEOUT_BASE 0x80000600ULL
#define N1X_VBIOS_CARVEOUT_SIZE 0x110000U

static unsigned int firmware_variant;
module_param(firmware_variant, uint, 0400);
MODULE_PARM_DESC(firmware_variant, "Pinned remapper-1 image: 0=initial, 1=fallback; no automatic retry (default 0)");

static bool opt_enable_irq = true;
module_param_named(enable_irq, opt_enable_irq, bool, 0400);
MODULE_PARM_DESC(enable_irq, "Enable SWGEN0/SWGEN1 interrupt mask state machine and waitqueue wakeup (default true)");

static bool enable_preempt = true;
module_param(enable_preempt, bool, 0400);
MODULE_PARM_DESC(enable_preempt, "Permit HAL-1 preemption enablement (0x31c) and resumable job control (default true)");

struct n1x_boot_context;

struct n1x_boot_endpoint {
	struct device *dev;
	struct n1x_boot_context *boot;
	void __iomem *base;
	void __iomem *hfrp;
	resource_size_t bytes;
	u32 first;
	bool sroot;
	int irq;
	struct device_link *sroot_link;
	struct dentry *debugfs;
	struct n1x_dla_accel *accel;
};

struct n1x_boot_preempt_state {
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
};

struct n1x_boot_shutdown_state {
	struct n1x_hfrp_stop_result transport;
	const char *stage;
	u32 irq_owned, irq_mask_before, irq_mask_after;
	bool irq_readback_valid;
	bool attempted, hardware_off, released;
	int error;
};

struct n1x_boot_hfrp_state {
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
};

/* Validated immutable VBIOS data, independent of the live HFRP channel. */
struct n1x_boot_board_policy {
	struct n1x_dla_vbios_policy policy;
	struct n1x_dla_gating_policy gating;
	struct n1x_dla_vbios_location location;
	bool valid;
};

struct n1x_boot_ping_state {
	void *buffer;
	dma_addr_t dma;
	u32 owned, mask_before, masked, word;
	bool attempted, verified;
	int error;
};

struct n1x_boot_config_state {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	u8 data[252];
	bool attempted, valid;
	int error;
};

struct n1x_boot_region5_state {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	bool attempted, registered;
	int error;
};

struct n1x_boot_config_apply_state {
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
};

struct n1x_boot_lpwr_state {
	void *buffer;
	dma_addr_t dma;
	struct n1x_dla_control_result transaction;
	bool attempted, accepted;
	int error;
};

struct n1x_sroot_client_buffer {
	void *cpu;
	dma_addr_t dma;
	size_t bytes;
	bool published, staging_released, sleep_retained;
};

struct n1x_sroot_client {
	struct n1x_boot_context *boot;
	struct device *supplier, *consumer;
	struct device_link *link;
	bool reserved, published, retention, ready, asleep;
	u32 image_id;
	u64 reinit_generation;
};

struct n1x_boot_context {
	struct kref refs;
	struct list_head entry;
	struct device *dev, *consumer, *dma_dev;
	struct fwnode_handle *scope;
	struct mutex
		lock; /* Serializes this provider's boot, DMA, queue and PM state. */
	wait_queue_head_t waitq;
	atomic_t irq_count;
	struct n1x_boot_endpoint *dla, *sroot;
	bool irq_registered, registered, removing, lifetime_held;
	unsigned int client_leases, retention_leases, ready_clients, sleeping_clients;
	struct n1x_dla_sroot_load_result upload_result;
	const struct firmware *firmware;
	size_t image_bytes;
	void *image;
	dma_addr_t image_dma;
	int upload_error;
	bool upload_attempted, stopping, hfrp_held, upload_held;
	bool sleep_prepared, suspended, loader_poisoned;
	u64 sleep_generation;
	struct n1x_boot_shutdown_state shutdown;
	struct n1x_boot_hfrp_state hfrp;
	struct n1x_boot_board_policy board_policy;
	struct n1x_boot_ping_state ping;
	struct n1x_boot_config_state config;
	struct n1x_boot_region5_state region5;
	struct n1x_boot_config_apply_state config_apply;
	struct n1x_boot_lpwr_state lpwr;
	struct n1x_boot_preempt_state preempt;
	struct n1x_dla_job task;
	struct n1x_dla_job_queue queue;
	struct n1x_sroot_client_buffer dsp_buffers[3];
};

static DEFINE_MUTEX(n1x_contexts_lock);
static LIST_HEAD(n1x_contexts);

static void n1x_boot_release(struct kref *refs)
{
	struct n1x_boot_context *boot =
		container_of(refs, struct n1x_boot_context, refs);

	put_device(boot->consumer);
	put_device(boot->dma_dev);
	release_firmware(boot->firmware);
	fwnode_handle_put(boot->scope);
	put_device(boot->dev);
	kfree(boot);
}

static void n1x_boot_put_ref(struct n1x_boot_context *boot)
{
	if (!IS_ERR_OR_NULL(boot))
		kref_put(&boot->refs, n1x_boot_release);
}

DEFINE_FREE(n1x_boot_put, struct n1x_boot_context *, n1x_boot_put_ref(_T))

static struct n1x_boot_context *n1x_boot_get(struct device *dev)
{
	struct n1x_boot_context *boot, *found = NULL;

	mutex_lock(&n1x_contexts_lock);
	list_for_each_entry(boot, &n1x_contexts, entry) {
		if (boot->dev == dev || boot->consumer == dev) {
			kref_get(&boot->refs);
			found = boot;
			break;
		}
	}
	mutex_unlock(&n1x_contexts_lock);
	return found;
}

/* Legacy kernel clients cannot select a device. Reject ambiguity explicitly. */
static struct n1x_boot_context *n1x_boot_get_unique(void)
{
	struct n1x_boot_context *boot, *found = NULL;

	mutex_lock(&n1x_contexts_lock);
	list_for_each_entry(boot, &n1x_contexts, entry) {
		if (READ_ONCE(boot->removing))
			continue;
		if (found) {
			n1x_boot_put_ref(found);
			found = ERR_PTR(-ENOTUNIQ);
			break;
		}
		kref_get(&boot->refs);
		found = boot;
	}
	mutex_unlock(&n1x_contexts_lock);
	return found ? found : ERR_PTR(-EPROBE_DEFER);
}

static void n1x_boot_hold_publication(struct n1x_boot_context *boot)
{
	if (!boot->lifetime_held) {
		kref_get(&boot->refs);
		boot->dma_dev = get_device(boot->dla->dev);
		boot->lifetime_held = true;
	}
}

/* Caller holds boot->lock and has established complete DMA quiescence. */
static void n1x_boot_drop_publication(struct n1x_boot_context *boot)
{
	if (boot->lifetime_held) {
		put_device(boot->dma_dev);
		boot->dma_dev = NULL;
		boot->lifetime_held = false;
		n1x_boot_put_ref(boot);
	}
}

static void n1x_boot_unregister(void *data)
{
	struct n1x_boot_context *boot = data;

	mutex_lock(&boot->lock);
	WRITE_ONCE(boot->removing, true);
	WRITE_ONCE(boot->stopping, true);
	boot->dla = NULL;
	boot->sroot = NULL;
	boot->irq_registered = false;
	mutex_unlock(&boot->lock);
	mutex_lock(&n1x_contexts_lock);
	if (boot->registered) {
		list_del(&boot->entry);
		boot->registered = false;
	}
	mutex_unlock(&n1x_contexts_lock);
}

static void n1x_boot_owner_put(void *data)
{
	struct n1x_boot_context *boot = data;

	n1x_boot_unregister(boot);
	n1x_boot_put_ref(boot);
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
		!spec || spec->num_ids < 1)
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
	/* DLA access uses only the HAL-1 route/mask/method/mailbox registers;
	 * core boot and reset are driven via SROOT and HFRP.
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

/* Top-half ISR: mask owned active bits via 0xd24 to silence the
 * level-triggered GIC line (GSI 234) and wake any waiting poller.
 */
static irqreturn_t n1x_boot_irq_handler(int irq, void *data)
{
	struct n1x_boot_endpoint *endpoint = data;
	struct n1x_boot_context *boot = endpoint ? READ_ONCE(endpoint->boot) :
						   NULL;
	u32 pending = 0, owned = 0, saved = 0;

	(void)irq;
	if (!endpoint || endpoint->sroot || !boot ||
	    !READ_ONCE(boot->hfrp.powered) || READ_ONCE(boot->stopping))
		return IRQ_NONE;
	if (n1x_boot_read(endpoint, 8, &pending) ||
		n1x_boot_read(endpoint, 0xd2c, &owned) ||
		n1x_boot_read(endpoint, 0xd28, &saved))
		return IRQ_NONE;
	if (!(pending & (owned & saved)) && !(pending & 0xc0))
		return IRQ_NONE;
	n1x_boot_write(endpoint, 0xd24, saved & owned);
	atomic_inc(&boot->irq_count);
	wake_up_all(&boot->waitq);
	return IRQ_HANDLED;
}

static void n1x_boot_delay(void *context, u32 interval_us)
{
	struct n1x_boot_endpoint *endpoint = context;
	struct n1x_boot_context *boot = endpoint ? READ_ONCE(endpoint->boot) :
						   NULL;

	if (boot && opt_enable_irq && boot->irq_registered &&
	    context == boot->dla && READ_ONCE(boot->hfrp.powered) &&
	    !READ_ONCE(boot->stopping) && interval_us >= 1000) {
		int before = atomic_read(&boot->irq_count);
		u32 owned = 0, saved = 0;

		if (!n1x_boot_read(boot->dla, 0xd2c, &owned) &&
		    !n1x_boot_read(boot->dla, 0xd28, &saved))
			n1x_boot_write_irq_enable(boot->dla,
						  saved | (0xc0U & owned));
		wait_event_timeout(boot->waitq,
				   atomic_read(&boot->irq_count) != before,
				   max(1UL, usecs_to_jiffies(interval_us)));
		return;
	}
	/* HFRP establishes DLA power. Its early polls must not access the DLA
	 * interrupt registers, and shutdown must not re-enable those interrupts.
	 */
	usleep_range(interval_us, interval_us + 100);
}

static int n1x_boot_board_policy(struct n1x_boot_context *boot,
				 const void *data, size_t size)
{
	int err;

	if (!data || !size || size > N1X_VBIOS_CARVEOUT_SIZE)
		return -EBADMSG;
	err = n1x_dla_parse_vbios_policy(data, size, &boot->hfrp.policy,
					 &boot->hfrp.location);
	if (err)
		return err;
	err = n1x_dla_verify_vbios_pcir(data, size, &boot->hfrp.location);
	if (err)
		return err;
	return n1x_dla_prepare_gating(&boot->hfrp.policy, &boot->hfrp.gating);
}

static int n1x_boot_live_board_policy(struct n1x_boot_context *boot)
{
	struct n1x_boot_board_policy *cached = &boot->board_policy;
	void __iomem *locator_io;
	resource_size_t rom_phys;
	u32 locator;
	void *rom;
	int err;

	/* Resume must not touch display MMIO before the GPU has resumed. The
	 * board ROM policy does not change during this provider's lifetime.
	 * Only parsed, PCIR-verified data is retained; HFRP state is rebuilt.
	 */
	if (cached->valid) {
		boot->hfrp.policy = cached->policy;
		boot->hfrp.gating = cached->gating;
		boot->hfrp.location = cached->location;
		return 0;
	}
	if (boot->sleep_prepared)
		return -EHOSTDOWN;
	locator_io = ioremap(N1X_DISP_VBIOS_LOCATOR_PHYS, sizeof(u32));
	if (!locator_io)
		return -ENOMEM;
	locator = readl(locator_io);
	iounmap(locator_io);
	rom_phys = (((u64)locator << 12) + N1X_VBIOS_CARVEOUT_BASE) & 0xffffffffULL;
	if (!rom_phys ||
		region_intersects(rom_phys, N1X_VBIOS_CARVEOUT_SIZE, IORESOURCE_SYSTEM_RAM,
					IORES_DESC_NONE) != REGION_DISJOINT)
		return -EOPNOTSUPP;
	rom = memremap(rom_phys, N1X_VBIOS_CARVEOUT_SIZE, MEMREMAP_WB);
	if (!rom)
		return -ENOMEM;
	err = n1x_boot_board_policy(boot, rom, N1X_VBIOS_CARVEOUT_SIZE);
	memunmap(rom);
	if (!err) {
		cached->policy = boot->hfrp.policy;
		cached->gating = boot->hfrp.gating;
		cached->location = boot->hfrp.location;
		cached->valid = true;
	}
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

/* Caller holds boot->lock. Hold PM/module lifetime from BEFORE the first write,
 * on failure as well as success. No retry, reset, IRQ enable or SROOT upload.
 * Unknown asynchronous responses fail closed in the common channel code.
 */
static int n1x_boot_prepare_hfrp_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u32 stamp;
	int err;

	if (!boot->dla || !boot->dla->hfrp || !boot->sroot)
		return -ENODEV;
	if (boot->hfrp.attempted || boot->upload_attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (!err)
		err = n1x_boot_dma_gate(boot->sroot);
	if (err)
		return err;
	err = n1x_boot_live_board_policy(boot);
	if (err)
		return err;
	err = pm_runtime_resume_and_get(boot->dla->dev);
	if (err < 0)
		return err;
	err = n1x_boot_hfrp_read(boot->dla, 0, &stamp);
	if (!err && stamp < 0x20250425)
		err = -EOPNOTSUPP;
	if (err)
		goto put;
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto put;
	}
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_hfrp_write,
				  n1x_boot_hfrp_read, n1x_boot_delay };
	n1x_boot_hold_publication(boot);
	boot->hfrp.attempted = true;
	boot->hfrp_held = true;
	boot->hfrp.phase = 1;
	err = n1x_hfrp_handshake(&io, &boot->hfrp.channel, 5001, 1000,
				 &boot->hfrp.exchange);
	if (err)
		goto done;
	boot->hfrp.phase = 2;
	err = n1x_hfrp_discover_version(&io, &boot->hfrp.channel, 5001, 1000,
					&boot->hfrp.version,
					&boot->hfrp.exchange);
	if (!err &&
	    (boot->hfrp.version.major != 1 || boot->hfrp.version.minor < 5))
		err = -EOPNOTSUPP;
	if (err)
		goto done;
	boot->hfrp.phase = 3;
	err = n1x_hfrp_configure_policy(&io, &boot->hfrp.channel,
					&boot->hfrp.version, &boot->hfrp.policy,
					5001, 1000, &boot->hfrp.setup);
	if (err)
		goto done;
	boot->hfrp.phase = 4;
	err = n1x_hfrp_power_on(&io, &boot->hfrp.channel, &boot->hfrp.version,
				boot->hfrp.gating.policy_bytes[2], 5001, 1000,
				&boot->hfrp.exchange);
	if (!err) {
		WRITE_ONCE(boot->hfrp.powered, true);
		boot->hfrp.phase = 5;
	}
done:
	dev_info(boot->dla->dev,
		 "HFRP phase=%u error=%d version=%u.%u command=%u completed=%u setup_reply=%#x/%u final_reply=%#x/%u power_accepted=%u; retained until checked shutdown\n",
		boot->hfrp.phase, err, boot->hfrp.version.major,
		boot->hfrp.version.minor, boot->hfrp.setup.command,
		boot->hfrp.setup.completed_commands,
		boot->hfrp.setup.exchange.reply.header,
		boot->hfrp.setup.exchange.reply.length,
		boot->hfrp.exchange.reply.header,
		boot->hfrp.exchange.reply.length, boot->hfrp.powered);
	return err;
put:
	pm_runtime_put(boot->dla->dev);
	return err;
}

static int n1x_boot_validate_firmware(const struct firmware *firmware)
{
	if (firmware_variant > 1)
		return -EINVAL;
	if (!firmware || !firmware->data || firmware->size < 4 ||
	    n1x_sroot_image_u32(firmware->data) != 0xc0302)
		return -EBADMSG;
	return n1x_sroot_validate_image(firmware->data + 4, firmware->size - 4, 0xc0302);
}

static int n1x_sroot_upload_dsp(struct n1x_boot_context *boot, u32 image_id,
				const u8 *image, size_t bytes,
				struct n1x_sroot_client_result *result,
				bool release_staging, bool *staging_released)
{
	struct n1x_sroot_client_buffer *buffer;
	struct n1x_sroot_reply reply = {};
	struct n1x_dla_io io;
	u32 words[3], index;
	int err;

	if (!result)
		return -EINVAL;
	memset(result, 0, sizeof(*result));
	if (staging_released)
		*staging_released = false;
	if (image_id < 0xc0303 || image_id > 0xc0305 || !image)
		return -EINVAL;
	index = image_id - 0xc0303;
	err = n1x_sroot_validate_image(image, bytes, image_id);
	if (err)
		return err;
	mutex_lock(&boot->lock);
	buffer = &boot->dsp_buffers[index];
	if (!boot->sroot || !boot->dla) {
		err = -ENODEV;
		goto unlock;
	}
	if (boot->removing || boot->sleep_prepared) {
		err = -EBUSY;
		goto unlock;
	}
	if (boot->stopping) {
		err = -ESHUTDOWN;
		goto unlock;
	}
	if (boot->loader_poisoned) {
		err = -ENOTRECOVERABLE;
		goto unlock;
	}
	if (buffer->published) {
		err = -EALREADY;
		goto unlock;
	}
	/* Do not begin on a failed or incomplete NPU secure-loader exchange. */
	if (!boot->upload_attempted || boot->upload_error ||
	    !boot->upload_result.reply_valid ||
	    boot->upload_result.reply.status ||
	    boot->upload_result.reply.detail) {
		err = -EHOSTDOWN;
		goto unlock;
	}
	err = n1x_boot_dma_gate(boot->sroot);
	if (err)
		goto unlock;
	err = dma_set_mask_and_coherent(boot->sroot->dev, DMA_BIT_MASK(32));
	if (err)
		goto unlock;
	err = pm_runtime_resume_and_get(boot->sroot->dev);
	if (err < 0)
		goto unlock;
	buffer->bytes = bytes + 4;
	buffer->cpu = dma_alloc_coherent(boot->sroot->dev, buffer->bytes,
					 &buffer->dma, GFP_KERNEL);
	if (!buffer->cpu) {
		err = -ENOMEM;
		goto put;
	}
	memcpy(buffer->cpu + 4, image, bytes);
	for (index = 0; index < 4; index++)
		((u8 *)buffer->cpu)[index] = image_id >> (index * 8);
	err = buffer->dma & 255 ? -ERANGE : n1x_sroot_stage_image(buffer->cpu,
		buffer->bytes, buffer->cpu, buffer->bytes, buffer->dma,
		0xe0000000, 0xffffffff, words);
	if (err)
		goto free;
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto free;
	}
	io = (struct n1x_dla_io){ boot->sroot, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	buffer->published = true;
	result->published = 1;
	err = n1x_sroot_exchange(&io, 16, N1X_SROOT_CMD_UPLOAD, words, 3,
				 &reply, 5001, 1000);
	if (!err) {
		result->reply_valid = 1;
		result->reply_header = reply.header;
		if (reply.command != N1X_SROOT_CMD_UPLOAD || reply.status || reply.detail)
			err = -EREMOTEIO;
		else if (release_staging && (reply.header != N1X_SROOT_CMD_UPLOAD || reply.count))
			err = -EPROTO;
		else
			result->accepted = 1;
	}
	if (!err && release_staging) {
		/* A completed upload consumes the image, not the DSP lifetime lease. */
		dma_free_coherent(boot->sroot->dev, buffer->bytes, buffer->cpu,
				  buffer->dma);
		buffer->cpu = NULL;
		buffer->dma = 0;
		buffer->staging_released = true;
		*staging_released = true;
	}
	if (err)
		boot->loader_poisoned = true;
	dev_info(boot->sroot->dev,
		 "DSP image %#x: error=%d reply_valid=%u header=%#x staging_released=%u; provider lease retained\n",
		image_id, err, result->reply_valid, result->reply_header,
		buffer->staging_released);
	goto unlock;
free:
	dma_free_coherent(boot->sroot->dev, buffer->bytes, buffer->cpu,
			  buffer->dma);
	memset(buffer, 0, sizeof(*buffer));
put:
	pm_runtime_put(boot->sroot->dev);
unlock:
	mutex_unlock(&boot->lock);
	return err;
}

int n1x_sroot_upload_dsp_v1(u32 image_id, const u8 *image, size_t bytes,
			  struct n1x_sroot_client_result *result)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = NULL;

	if (!result)
		return -EINVAL;
	boot = n1x_boot_get_unique();
	if (IS_ERR(boot)) {
		memset(result, 0, sizeof(*result));
		return PTR_ERR(boot);
	}
	return n1x_sroot_upload_dsp(boot, image_id, image, bytes, result, false,
				    NULL);
}
EXPORT_SYMBOL_GPL(n1x_sroot_upload_dsp_v1);

int n1x_sroot_upload_dsp_v2(u32 image_id, const u8 *image, size_t bytes,
			  struct n1x_sroot_client_result_v2 *result)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = NULL;
	struct n1x_sroot_client_result upload;
	bool released = false;
	int err;

	if (!result)
		return -EINVAL;
	memset(result, 0, sizeof(*result));
	boot = n1x_boot_get_unique();
	if (IS_ERR(boot))
		return PTR_ERR(boot);
	err = n1x_sroot_upload_dsp(boot, image_id, image, bytes, &upload, true,
				   &released);
	result->reply_header = upload.reply_header;
	result->published = upload.published;
	result->reply_valid = upload.reply_valid;
	result->accepted = upload.accepted;
	result->staging_released = released;
	return err;
}
EXPORT_SYMBOL_GPL(n1x_sroot_upload_dsp_v2);

/* Caller holds boot->lock. No DSP address may be published without this gate. */
static int n1x_sroot_client_ready(struct n1x_boot_context *boot)
{
	if (boot->removing || !boot->sroot || !boot->dla)
		return -ENODEV;
	if (boot->stopping)
		return -ESHUTDOWN;
	if (boot->sleep_prepared)
		return -EBUSY;
	if (boot->loader_poisoned)
		return -ENOTRECOVERABLE;
	if (!boot->upload_attempted || boot->upload_error ||
	    !boot->upload_result.reply_valid ||
	    boot->upload_result.reply.status ||
	    boot->upload_result.reply.detail)
		return -EHOSTDOWN;
	return 0;
}

static struct n1x_sroot_client *n1x_sroot_client_get(struct device *dev)
{
	struct n1x_boot_context *boot, *found = NULL;
	struct fwnode_handle *scope;
	struct n1x_sroot_client *client;
	struct device *supplier = NULL;
	int err;

	if (!dev || !dev_fwnode(dev))
		return ERR_PTR(-EINVAL);
	scope = fwnode_get_parent(dev_fwnode(dev));
	if (!scope)
		return ERR_PTR(-ENODEV);
	mutex_lock(&n1x_contexts_lock);
	list_for_each_entry(boot, &n1x_contexts, entry) {
		if (READ_ONCE(boot->removing) || boot->scope != scope)
			continue;
		if (found) {
			n1x_boot_put_ref(found);
			put_device(supplier);
			found = ERR_PTR(-ENOTUNIQ);
			supplier = NULL;
			break;
		}
		kref_get(&boot->refs);
		found = boot;
		supplier = get_device(boot->consumer);
	}
	mutex_unlock(&n1x_contexts_lock);
	fwnode_handle_put(scope);
	if (IS_ERR(found))
		return ERR_CAST(found);
	if (!found)
		return ERR_PTR(-EPROBE_DEFER);
	boot = found;
	err = -EPROBE_DEFER;
	if (!supplier)
		goto put_boot;
	client = kzalloc_obj(*client);
	if (!client) {
		err = -ENOMEM;
		goto put_supplier;
	}
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto free;
	}
	/* Probe-time only: order DSP before DLA before SROOT during system PM.
	 * The mailbox is shared with the DLA firmware bootstrap contract.
	 */
	client->link = device_link_add(dev, supplier, DL_FLAG_STATELESS);
	if (!client->link) {
		err = -ENOMEM;
		goto put_module;
	}
	mutex_lock(&boot->lock);
	err = n1x_sroot_client_ready(boot);
	if (!err && boot->dla->dev != supplier)
		err = -ENODEV;
	mutex_unlock(&boot->lock);
	if (err) {
		device_link_del(client->link);
		if (err == -EHOSTDOWN)
			err = -EPROBE_DEFER;
		goto put_module;
	}
	client->boot = boot;
	client->supplier = supplier;
	client->consumer = get_device(dev);
	return client;
put_module:
	module_put(THIS_MODULE);
free:
	kfree(client);
put_supplier:
	put_device(supplier);
put_boot:
	n1x_boot_put_ref(boot);
	return ERR_PTR(err);
}

static int n1x_sroot_client_prepare(struct n1x_sroot_client *client)
{
	struct n1x_boot_context *boot = client->boot;
	int err;

	mutex_lock(&boot->lock);
	err = n1x_sroot_client_ready(boot);
	if (!err && boot->dla->dev != client->supplier)
		err = -ENODEV;
	if (!err && client->reserved)
		err = -EALREADY;
	if (!err) {
		client->reserved = true;
		boot->client_leases++;
		if (client->retention)
			boot->retention_leases++;
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_sroot_client_publish(struct n1x_sroot_client *client)
{
	struct n1x_boot_context *boot = client->boot;
	int err;

	mutex_lock(&boot->lock);
	err = n1x_sroot_client_ready(boot);
	if (!err && boot->dla->dev != client->supplier)
		err = -ENODEV;
	if (!err && !client->reserved)
		err = -EINVAL;
	if (!err && client->published)
		err = -EALREADY;
	if (!err)
		client->published = true;
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_sroot_client_cancel(struct n1x_sroot_client *client)
{
	struct n1x_boot_context *boot = client->boot;
	int err = 0;

	mutex_lock(&boot->lock);
	if (client->published) {
		err = -EBUSY;
	} else if (client->reserved) {
		client->reserved = false;
		boot->client_leases--;
		if (client->retention)
			boot->retention_leases--;
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_sroot_client_put(struct n1x_sroot_client *client)
{
	int err = n1x_sroot_client_cancel(client);

	/* There is no verified DSP shutdown yet. Keep the handle and its module,
	 * devices and dependency alive after any DSP DMA publication.
	 */
	if (err)
		return err;
	device_link_del(client->link);
	put_device(client->consumer);
	put_device(client->supplier);
	n1x_boot_put_ref(client->boot);
	kfree(client);
	module_put(THIS_MODULE);
	return 0;
}

static int n1x_sroot_client_upload(struct n1x_sroot_client *client,
				   u32 image_id, const u8 *image, size_t bytes,
				   struct n1x_sroot_client_result_v2 *result)
{
	struct n1x_boot_context *boot = client->boot;
	struct n1x_sroot_client_result upload;
	bool published, released = false;
	int err;

	if (!result)
		return -EINVAL;
	memset(result, 0, sizeof(*result));
	mutex_lock(&boot->lock);
	if (client->asleep) {
		mutex_unlock(&boot->lock);
		return -EBUSY;
	}
	published = client->published;
	mutex_unlock(&boot->lock);
	if (!published)
		return -EPERM;
	err = n1x_sroot_upload_dsp(boot, image_id, image, bytes, &upload, true,
				   &released);
	result->reply_header = upload.reply_header;
	result->published = upload.published;
	result->reply_valid = upload.reply_valid;
	result->accepted = upload.accepted;
	result->staging_released = released;
	if (!err && released && client->retention) {
		mutex_lock(&boot->lock);
		if (!client->image_id)
			client->image_id = image_id;
		boot->dsp_buffers[image_id - 0xc0303].sleep_retained = true;
		if (!client->ready) {
			client->ready = true;
			boot->ready_clients++;
		}
		mutex_unlock(&boot->lock);
	}
	return err;
}

static const struct n1x_sroot_client_ops n1x_sroot_ops = {
	.get = n1x_sroot_client_get,
	.put = n1x_sroot_client_put,
	.prepare = n1x_sroot_client_prepare,
	.publish = n1x_sroot_client_publish,
	.cancel = n1x_sroot_client_cancel,
	.upload = n1x_sroot_client_upload,
};

const struct n1x_sroot_client_ops *n1x_sroot_client_ops_v3(void)
{
	return &n1x_sroot_ops;
}
EXPORT_SYMBOL_GPL(n1x_sroot_client_ops_v3);

static struct n1x_sroot_client *n1x_sroot_client_get_v4(struct device *dev)
{
	struct n1x_sroot_client *client = n1x_sroot_client_get(dev);

	if (!IS_ERR(client))
		client->retention = true;
	return client;
}

static int n1x_sroot_client_suspend(struct n1x_sroot_client *client)
{
	struct n1x_boot_context *boot = client->boot;
	int err = 0;

	mutex_lock(&boot->lock);
	if (!client->retention || !client->published || !client->ready || !boot->sleep_prepared) {
		err = -EINVAL;
	} else if (boot->removing || !boot->dla || !boot->sroot ||
		   boot->dla->dev != client->supplier) {
		err = -ENODEV;
	} else if (boot->stopping || boot->loader_poisoned) {
		err = -ENOTRECOVERABLE;
	} else if (client->asleep) {
		err = -EALREADY;
	} else {
		client->asleep = true;
		boot->sleeping_clients++;
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_sroot_client_resume(struct n1x_sroot_client *client)
{
	struct n1x_boot_context *boot = client->boot;
	int err = 0;

	mutex_lock(&boot->lock);
	if (!client->asleep) {
		err = -EINVAL;
	} else if (boot->removing || !boot->dla || !boot->sroot ||
		   boot->dla->dev != client->supplier) {
		err = -ENODEV;
	} else if (boot->suspended || boot->stopping || boot->loader_poisoned ||
		   boot->upload_error) {
		err = -EHOSTDOWN;
	} else {
		client->asleep = false;
		boot->sleeping_clients--;
	}
	mutex_unlock(&boot->lock);
	return err;
}

static const struct n1x_sroot_client_ops n1x_sroot_retention_client_ops = {
	.get = n1x_sroot_client_get_v4,
	.put = n1x_sroot_client_put,
	.prepare = n1x_sroot_client_prepare,
	.publish = n1x_sroot_client_publish,
	.cancel = n1x_sroot_client_cancel,
	.upload = n1x_sroot_client_upload,
};

static const struct n1x_sroot_client_sleep_ops n1x_sroot_sleep_ops = {
	.client = &n1x_sroot_retention_client_ops,
	.suspend = n1x_sroot_client_suspend,
	.resume = n1x_sroot_client_resume,
};

const struct n1x_sroot_client_sleep_ops *n1x_sroot_client_ops_v4(void)
{
	return &n1x_sroot_sleep_ops;
}
EXPORT_SYMBOL_GPL(n1x_sroot_client_ops_v4);

int n1x_sroot_reinit_dsp_v1(struct n1x_sroot_client *client,
			    struct n1x_sroot_reinit_result *result)
{
	struct n1x_sroot_client_buffer *buffer;
	struct n1x_sroot_reply reply;
	struct n1x_boot_context *boot;
	struct n1x_dla_io io;
	u32 words[3];
	int err = 0;

	if (!result)
		return -EINVAL;
	memset(result, 0, sizeof(*result));
	if (!client)
		return -EINVAL;
	boot = client->boot;
	mutex_lock(&boot->lock);
	if (!client->retention || !client->published || !client->ready ||
	    client->image_id < 0xc0303 || client->image_id > 0xc0305 ||
	    (!client->asleep && !boot->sleep_prepared) || !boot->sleep_generation) {
		err = -EINVAL;
		goto unlock;
	}
	if (boot->removing || !boot->dla || !boot->sroot ||
	    boot->dla->dev != client->supplier) {
		err = -ENODEV;
		goto unlock;
	}
	if (boot->suspended || boot->stopping || boot->loader_poisoned || boot->upload_error) {
		err = -EHOSTDOWN;
		goto unlock;
	}
	if (client->reinit_generation == boot->sleep_generation) {
		err = -EALREADY;
		goto unlock;
	}
	buffer = &boot->dsp_buffers[client->image_id - 0xc0303];
	if (!buffer->published || !buffer->staging_released || !buffer->sleep_retained) {
		err = -ENOTRECOVERABLE;
		goto unlock;
	}
	/* Native NotifySrootReinitFw expands (image_id, mode=1) into three
	 * mailbox words: a zero-extended 64-bit image ID followed by the mode.
	 */
	words[0] = client->image_id;
	words[1] = 0;
	words[2] = 1;
	io = (struct n1x_dla_io){ boot->sroot, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	client->reinit_generation = boot->sleep_generation;
	result->attempted = 1;
	err = n1x_sroot_exchange(&io, 16, 0x1a, words, ARRAY_SIZE(words), &reply, 5001, 1000);
	if (!err) {
		result->reply_valid = 1;
		result->reply_header = reply.header;
		if (reply.command != 0x1a || reply.status || reply.detail)
			err = -EREMOTEIO;
		else if (reply.header != 0x1a || reply.count)
			err = -EPROTO;
		else
			result->accepted = 1;
	}
	if (err)
		boot->loader_poisoned = true;
unlock:
	mutex_unlock(&boot->lock);
	return err;
}
EXPORT_SYMBOL_GPL(n1x_sroot_reinit_dsp_v1);

/* Caller holds boot->lock. Keep the validated CPU image independent of DMA
 * staging so resume never depends on filesystem I/O or a userspace fallback.
 */
static int n1x_boot_cache_firmware(struct n1x_boot_context *boot)
{
	const char *name = firmware_variant ? N1X_BOOT_FALLBACK_FIRMWARE : N1X_BOOT_FIRMWARE;
	const struct firmware *firmware;
	int err;

	if (boot->firmware)
		return 0;
	if (boot->sleep_prepared)
		return -EHOSTDOWN;
	device_property_read_string(boot->sroot->dev, "firmware-name", &name);
	err = request_firmware_direct(&firmware, name, boot->sroot->dev);
	if (err)
		return err;
	err = n1x_boot_validate_firmware(firmware);
	if (err) {
		release_firmware(firmware);
		return err;
	}
	boot->firmware = firmware;
	return 0;
}

/* Caller holds boot->lock. */
static int n1x_boot_upload(struct n1x_boot_context *boot)
{
	const struct firmware *firmware;
	struct n1x_dla_io sroot_io, dla_io;
	u32 words[3];
	int err;

	if (!boot->sroot || !boot->dla)
		return -ENODEV;
	if (boot->loader_poisoned)
		return -ENOTRECOVERABLE;
	if (boot->upload_attempted)
		return -EALREADY;
	if (!boot->hfrp.powered)
		return -EHOSTDOWN;
	if (firmware_variant > 1)
		return -EINVAL;
	err = n1x_boot_dma_gate(boot->sroot);
	if (!err)
		err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	err = n1x_boot_cache_firmware(boot);
	if (err)
		return err;
	firmware = boot->firmware;
	err = dma_set_mask_and_coherent(boot->sroot->dev, DMA_BIT_MASK(32));
	if (err)
		goto release;
	err = pm_runtime_resume_and_get(boot->sroot->dev);
	if (err < 0) {
		dev_err(boot->sroot->dev,
			"runtime resume before upload failed: %d\n", err);
		goto release;
	}
	err = pm_runtime_resume_and_get(boot->dla->dev);
	if (err < 0) {
		dev_err(boot->dla->dev,
			"runtime resume before upload failed: %d\n", err);
		goto put_sroot;
	}
	boot->image_bytes = firmware->size;
	boot->image = dma_alloc_coherent(boot->sroot->dev, boot->image_bytes,
					 &boot->image_dma, GFP_KERNEL);
	if (!boot->image) {
		err = -ENOMEM;
		goto put_dla;
	}
	if (boot->image_dma & 255) {
		err = -ERANGE;
		goto free;
	}
	err = n1x_sroot_stage_image(boot->image, boot->image_bytes,
				    firmware->data, firmware->size,
				    boot->image_dma, 0xe0000000, 0xffffffff,
				    words);
	if (err)
		goto free;
	/* Acquire the lifetime reference before publication and release it only
	 * after checked shutdown. Retain it on timeout, rejection or idle failure.
	 */
	if (!try_module_get(THIS_MODULE)) {
		err = -ENODEV;
		goto free;
	}
	sroot_io = (struct n1x_dla_io){ boot->sroot, n1x_boot_write,
					n1x_boot_read, n1x_boot_delay };
	dla_io = (struct n1x_dla_io){ boot->dla, NULL, n1x_boot_read,
				      n1x_boot_delay };
	dma_wmb();
	boot->upload_attempted = true;
	boot->upload_held = true;
	err = n1x_dla_sroot_upload(&sroot_io, &dla_io, 16, words, 5001, 1000,
				   &boot->upload_result);
	dev_info(boot->dla->dev,
		 "upload variant %u phase returned %d: reply_valid=%u raw=%#x idle=%u; DMA %pad retained until checked shutdown\n",
		firmware_variant, err, boot->upload_result.reply_valid,
		boot->upload_result.reply.header,
		boot->upload_result.idle_observed, &boot->image_dma);
	/* The validated CPU image remains available for system resume. */
	return err;
free:
	dma_free_coherent(boot->sroot->dev, boot->image_bytes, boot->image,
			  boot->image_dma);
	boot->image = NULL;
	boot->image_dma = 0;
put_dla:
	pm_runtime_put(boot->dla->dev);
put_sroot:
	pm_runtime_put(boot->sroot->dev);
release:
	return err;
}

/* Caller holds boot->lock. The upload already pins module/runtime references.
 * A DMA-word change qualifies only command 1, never model execution/quiescence.
 */
static int n1x_boot_ping_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	u32 attempt;
	int err;

	if (!boot->sroot || !boot->dla || !boot->upload_attempted ||
	    !boot->upload_result.reply_valid ||
	    !boot->upload_result.idle_observed ||
	    boot->upload_result.reply.status ||
	    boot->upload_result.reply.detail)
		return -ENODEV;
	if (boot->ping.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot->dla->dev, maximum);
	if (err)
		return err;
	boot->ping.buffer = dma_alloc_coherent(boot->dla->dev, 4096,
					       &boot->ping.dma, GFP_KERNEL);
	if (!boot->ping.buffer)
		return -ENOMEM;
	if (!boot->ping.dma || (boot->ping.dma & 255) ||
	    boot->ping.dma > maximum || maximum - boot->ping.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot->ping.buffer, 0, 4096);
	WRITE_ONCE(*(__le32 *)boot->ping.buffer, cpu_to_le32(0x11111111));
	err = n1x_boot_read(boot->dla, 0xd2c, &boot->ping.owned);
	if (err)
		goto free;
	err = n1x_boot_read(boot->dla, 0xd28, &boot->ping.mask_before);
	if (err)
		goto free;
	boot->ping.masked = boot->ping.owned & boot->ping.mask_before;
	/* Conservative lifetime from the first mask-write attempt. No retries,
	 * auto-restoration or freeing once the phase can change device state.
	 */
	boot->ping.attempted = true;
	err = n1x_boot_write(boot->dla, 0xd24, boot->ping.masked);
	if (err)
		return err;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	/* Method 1 (flags=0) writes completion word 0x44444444 directly to the
	 * 256-byte-aligned DMA page without raising a SWGEN1 mailbox response.
	 */
	err = n1x_dla_send_method(&io, 1, boot->ping.dma >> 8, 5001, 1000);
	if (err)
		return err;
	for (attempt = 0; attempt < 5001; attempt++) {
		boot->ping.word =
			le32_to_cpu(READ_ONCE(*(__le32 *)boot->ping.buffer));
		if (boot->ping.word == 0x44444444) {
			dma_rmb();
			boot->ping.verified = true;
			return 0;
		}
		if (attempt + 1 < 5001)
			n1x_boot_delay(boot->dla, 1000);
	}
	return -ETIMEDOUT;
free:
	dma_free_coherent(boot->dla->dev, 4096, boot->ping.buffer,
			  boot->ping.dma);
	boot->ping.buffer = NULL;
	boot->ping.dma = 0;
	return err;
}

/* Caller holds boot->lock; snapshot export is not configuration mutation. */
static int n1x_boot_read_config_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!boot->sroot || !boot->dla || !boot->ping.verified)
		return -ENODEV;
	if (boot->config.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot->dla->dev, maximum);
	if (err)
		return err;
	boot->config.buffer = dma_alloc_coherent(boot->dla->dev, 4096,
						 &boot->config.dma, GFP_KERNEL);
	if (!boot->config.buffer)
		return -ENOMEM;
	if (!boot->config.dma || (boot->config.dma & 255) ||
	    boot->config.dma > maximum || maximum - boot->config.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot->config.buffer, 0, 4096);
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	/* Retain conservatively even on a stale-response/pre-send error. The
	 * preceding upload already pins module/runtime references until checked shutdown.
	 */
	boot->config.attempted = true;
	err = n1x_dla_control_command(&io, 22, boot->config.dma, 5001, 1000,
				      &boot->config.transaction);
	if (err)
		return err;
	dma_rmb();
	memcpy(boot->config.data, boot->config.buffer,
	       sizeof(boot->config.data));
	boot->config.valid = true;
	return 0;
free:
	dma_free_coherent(boot->dla->dev, 4096, boot->config.buffer,
			  boot->config.dma);
	boot->config.buffer = NULL;
	boot->config.dma = 0;
	return err;
}

/* Caller holds boot->lock; register the 1 MiB HAL-1 region-5 circular ring
 * descriptor via SET_REGIONS (command 11) before configuration import.
 */
static int n1x_boot_init_region5_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!boot->sroot || !boot->dla || !boot->ping.verified)
		return -ENODEV;
	if (boot->region5.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot->dla->dev, maximum);
	if (err)
		return err;
	boot->region5.buffer =
		dma_alloc_coherent(boot->dla->dev, N1X_REGION5_ALLOC,
				   &boot->region5.dma, GFP_KERNEL);
	if (!boot->region5.buffer)
		return -ENOMEM;
	if (!boot->region5.dma || (boot->region5.dma & 255) ||
	    boot->region5.dma > maximum ||
	    N1X_REGION5_ALLOC - 1 > maximum - boot->region5.dma) {
		err = -ERANGE;
		goto free;
	}
	memset(boot->region5.buffer, 0, N1X_REGION5_ALLOC);
	err = n1x_dla_build_region(boot->region5.buffer, N1X_REGION5_ALLOC, 5,
				   boot->region5.dma + N1X_REGION5_OFFSET,
				   N1X_REGION5_BYTES);
	if (err)
		goto free;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	/* The existing upload reference owns module/runtime lifetime. Do not
	 * release this descriptor/ring even after an accepted/ACKed command.
	 */
	boot->region5.attempted = true;
	err = n1x_dla_control_command(&io, 11, boot->region5.dma, 5001, 1000,
				      &boot->region5.transaction);
	if (!err)
		boot->region5.registered = true;
	return err;
free:
	dma_free_coherent(boot->dla->dev, N1X_REGION5_ALLOC,
			  boot->region5.buffer, boot->region5.dma);
	boot->region5.buffer = NULL;
	boot->region5.dma = 0;
	return err;
}

/* Reuse the retained config allocation so the saved CPU snapshot
 * remains the original pre-import export.
 */
static int n1x_boot_apply_config_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!boot->sroot || !boot->dla || !boot->ping.verified ||
	    !boot->region5.registered || !boot->config.valid ||
	    !boot->config.buffer)
		return -ENODEV;
	if (boot->config_apply.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	if (!boot->config.dma || (boot->config.dma & 255) ||
	    boot->config.dma > maximum || maximum - boot->config.dma < 4095)
		return -ERANGE;
	err = n1x_dla_prepare_default_config(boot->config.buffer, 4096);
	if (err)
		return err;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	boot->config_apply.attempted = true;
	err = n1x_dla_control_command(&io, 21, boot->config.dma, 5001, 1000,
				      &boot->config_apply.transaction);
	if (!err)
		boot->config_apply.accepted = true;
	/* No path frees or replaces the export's already-published allocation. */
	return err;
}

/* Preserve the board's VBIOS gating byte 0, substituting 4 only when zero.
 * Do not infer physical time units here.
 */
static int n1x_boot_apply_lpwr_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u64 maximum = DMA_BIT_MASK(36);
	int err;

	if (!boot->sroot || !boot->dla || !boot->ping.verified ||
	    !boot->region5.registered || !boot->config_apply.accepted ||
	    !boot->hfrp.powered || !boot->hfrp.policy.boot_tables_valid)
		return -ENODEV;
	if (boot->lpwr.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot->dla->dev, maximum);
	if (err)
		return err;
	boot->lpwr.buffer = dma_alloc_coherent(boot->dla->dev, 4096,
					       &boot->lpwr.dma, GFP_KERNEL);
	if (!boot->lpwr.buffer)
		return -ENOMEM;
	if (!boot->lpwr.dma || (boot->lpwr.dma & 255) ||
	    boot->lpwr.dma > maximum || maximum - boot->lpwr.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot->lpwr.buffer, 0, 4096);
	err = n1x_dla_build_command23(boot->lpwr.buffer, 4096,
				      boot->hfrp.gating.hal_flags[0], 1,
				      boot->hfrp.policy.gating_bytes[0] ?: 4);
	if (err)
		goto free;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	dma_wmb();
	/* Upload already holds runtime/module references. A reply or ACK cannot
	 * establish quiescence: retain this backing on every attempted command.
	 */
	boot->lpwr.attempted = true;
	err = n1x_dla_control_command(&io, 23, boot->lpwr.dma, 5001, 1000,
				      &boot->lpwr.transaction);
	if (!err)
		boot->lpwr.accepted = true;
	return err;
free:
	dma_free_coherent(boot->dla->dev, 4096, boot->lpwr.buffer,
			  boot->lpwr.dma);
	boot->lpwr.buffer = NULL;
	boot->lpwr.dma = 0;
	return err;
}

/* N1X hardware sends {word=12,data=0} after idle. Acknowledge this
 * notification without marking task completion. Keep the device powered
 * under the existing runtime reference; do not infer or execute a power-down
 * policy from this notification. Firmware holds a mailbox until ACK, and
 * boot->lock excludes another reader.
 */
static int n1x_boot_drain_task_idle(struct n1x_boot_context *boot,
				    struct n1x_dla_job *task)
{
	struct n1x_dla_io io = { boot->dla, n1x_boot_write, n1x_boot_read,
				 n1x_boot_delay };
	struct n1x_dla_response response;
	u32 word, data, i;
	int err;

	if (boot->queue.poisoned || task->idle_blocked)
		return -ENOTRECOVERABLE;
	if (boot->queue.active)
		return -EBUSY;
	for (i = 0; i < 16; i++) {
		err = n1x_boot_read(boot->dla, 8, &task->pending);
		if (err)
			return err;
		if (!(task->pending & 0xc0))
			return 0;
		if (task->pending & 0x40)
			return -EBUSY;
		err = n1x_boot_read(boot->dla, 0x40, &word);
		if (!err)
			err = n1x_boot_read(boot->dla, 0x44, &data);
		if (err)
			return err;
		if (word != 12 || data != 0)
			return -EBUSY; /* Unknown/error/stale response remains unacknowledged. */
		task->idle_blocked = true; /* An uncertain ACK cannot be replayed. */
		err = n1x_dla_take_response(&io, &response);
		if (err) {
			boot->queue.poisoned = true;
			return err;
		}
		if (response.word != 12 || response.data != 0) {
			boot->queue.poisoned = true;
			return -EPROTO;
		}
		task->idle_notifications++;
		task->idle_blocked = false;
	}
	task->idle_blocked = true;
	boot->queue.poisoned = true;
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

static int n1x_boot_run_task(struct n1x_boot_context *boot,
			     struct n1x_dla_job *task)
{
	struct n1x_dla_io io = { boot->dla, n1x_boot_write, n1x_boot_read,
				 n1x_boot_delay };
	struct n1x_dla_job_ops ops = {task, n1x_boot_publish_job, n1x_boot_read_job_fence};

	return n1x_dla_job_execute(&boot->queue, &io, &ops, task);
}

static int n1x_boot_bypass_task_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_task_args args = {0};
	u64 maximum = DMA_BIT_MASK(36), network;
	int err;

	if (!boot->sroot || !boot->dla || !boot->ping.verified ||
	    !boot->region5.registered || !boot->config_apply.accepted ||
	    !boot->lpwr.accepted)
		return -ENODEV;
	if (boot->task.attempted)
		return -EALREADY;
	err = n1x_boot_dma_gate(boot->dla);
	if (err)
		return err;
	if (boot->queue.last_fence == U32_MAX)
		return -EOVERFLOW;
	err = n1x_boot_drain_task_idle(boot, &boot->task);
	if (err)
		return err;
	err = dma_set_mask_and_coherent(boot->dla->dev, maximum);
	if (err)
		return err;
	boot->task.buffer = dma_alloc_coherent(boot->dla->dev, 4096,
					       &boot->task.dma, GFP_KERNEL);
	if (!boot->task.buffer)
		return -ENOMEM;
	if (!boot->task.dma || (boot->task.dma & 255) ||
	    boot->task.dma > maximum || maximum - boot->task.dma < 4095) {
		err = -ERANGE;
		goto free;
	}
	memset(boot->task.buffer, 0, 4096);
	args.address_list = boot->task.dma + 256;
	args.num_addresses = 1;
	network = boot->task.dma + 512;
	err = n1x_dla_build_address_list(boot->task.buffer + 256, 256, &network,
					 1);
	if (!err)
		err = n1x_dla_build_bypass_fenced_task_v2(boot->task.buffer,
							  256, &args,
							  boot->task.dma + 768,
							  0x12345678);
	if (err)
		goto free;
	WRITE_ONCE(*(__le32 *)(boot->task.buffer + 768),
		   cpu_to_le32(0xdeadbeef));
	boot->task.fence = 0xdeadbeef;
	err = n1x_boot_run_task(boot, &boot->task);
	if (err && !boot->task.attempted)
		goto free;
	return err;
free:
	dma_free_coherent(boot->dla->dev, 4096, boot->task.buffer,
			  boot->task.dma);
	boot->task.buffer = NULL;
	boot->task.dma = 0;
	return err;
}

/* Explicit diagnostic only. Successful preparation already pins runtime PM
 * and module lifetime. Serialize with startup, inference and other queries.
 * Reading hfrp_status never sends a command or consumes a mailbox response.
 */
static ssize_t query_power_state_store(struct device *dev, struct device_attribute *attr,
						const char *buf, size_t count)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "query"))
		return -EPERM;
	mutex_lock(&boot->lock);
	if (boot->removing || boot->sleep_prepared) {
		err = -EBUSY;
	} else if (boot->stopping) {
		err = -ESHUTDOWN;
	} else if (!boot->dla || boot->dla->dev != dev || !boot->dla->hfrp ||
		 !boot->sroot) {
		err = -ENODEV;
	} else if (!boot->hfrp.attempted || !boot->hfrp.powered ||
		 boot->hfrp.error) {
		err = -EHOSTDOWN;
	} else if (boot->hfrp.channel.state != N1X_HFRP_READY) {
		err = boot->hfrp.channel.state == N1X_HFRP_FAILED ?
			      -ENOTRECOVERABLE :
			      -EBUSY;
	} else {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_hfrp_write,
					  n1x_boot_hfrp_read, n1x_boot_delay };
		boot->hfrp.state_attempted = true;
		memset(&boot->hfrp.power_state, 0,
		       sizeof(boot->hfrp.power_state));
		memset(&boot->hfrp.state_exchange, 0,
		       sizeof(boot->hfrp.state_exchange));
		err = n1x_hfrp_query_power_state(&io, &boot->hfrp.channel,
						 &boot->hfrp.version, 5001,
						 1000, &boot->hfrp.power_state,
						 &boot->hfrp.state_exchange);
		boot->hfrp.state_error = err;
		dev_info(dev,
			 "HFRP state query error=%d valid=%u raw=%#x reply=%#x length=%u channel=%u; no idle guarantee\n",
			err, boot->hfrp.power_state.valid,
			boot->hfrp.power_state.raw,
			boot->hfrp.state_exchange.reply.header,
			boot->hfrp.state_exchange.reply.length,
			boot->hfrp.channel.state);
	}
	mutex_unlock(&boot->lock);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(query_power_state);

static ssize_t hfrp_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	ssize_t size;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (!boot->dla || boot->dla->dev != dev) {
		mutex_unlock(&boot->lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
			  "attempted=%u phase=%u error=%d state=%u pending=%u stamp=%#x version=%u.%u configured=%u command=%u completed=%u setup_reply=%#x setup_length=%u setup_payload=%*phN final_reply=%#x final_length=%u final_payload=%*phN power_accepted=%u frequency_khz=%u gating=%#x query_attempted=%u query_error=%d query_valid=%u query_raw=%#x query_reply=%#x query_length=%u query_payload=%*phN\n",
		boot->hfrp.attempted, boot->hfrp.phase, boot->hfrp.error,
		boot->hfrp.channel.state, boot->hfrp.channel.pending_sequence,
		boot->hfrp.version.stamp, boot->hfrp.version.major,
		boot->hfrp.version.minor, boot->hfrp.setup.configured,
		boot->hfrp.setup.command, boot->hfrp.setup.completed_commands,
		boot->hfrp.setup.exchange.reply.header,
		boot->hfrp.setup.exchange.reply.length,
		(int)boot->hfrp.setup.exchange.reply.length,
		boot->hfrp.setup.exchange.reply.payload,
		boot->hfrp.exchange.reply.header,
		boot->hfrp.exchange.reply.length,
		(int)boot->hfrp.exchange.reply.length,
		boot->hfrp.exchange.reply.payload, boot->hfrp.powered,
		boot->hfrp.policy.frequency_khz, boot->hfrp.gating.request_mask,
		boot->hfrp.state_attempted, boot->hfrp.state_error,
		boot->hfrp.power_state.valid, boot->hfrp.power_state.raw,
		boot->hfrp.state_exchange.reply.header,
		boot->hfrp.state_exchange.reply.length,
		(int)boot->hfrp.state_exchange.reply.length,
		boot->hfrp.state_exchange.reply.payload);
	mutex_unlock(&boot->lock);
	return size;
}

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	ssize_t size;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (!boot->dla || boot->dla->dev != dev) {
		mutex_unlock(&boot->lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
			  "dla_bound=%u sroot_bound=%u attempted=%u error=%d reply_valid=%u reply=%#x idle_observed=%u dma_retained=%u ping_attempted=%u ping_error=%d ping_verified=%u ping_word=%#x ping_retained=%u config_attempted=%u config_error=%d config_valid=%u config_reply_valid=%u config_reply=%#x config_retained=%u region5_attempted=%u region5_error=%d region5_registered=%u region5_retained=%u config_apply_attempted=%u config_apply_error=%d config_apply_accepted=%u config_apply_reply_valid=%u config_apply_reply=%#x lpwr_attempted=%u lpwr_error=%d lpwr_accepted=%u lpwr_reply_valid=%u lpwr_reply=%#x lpwr_retained=%u task_attempted=%u task_error=%d task_response_valid=%u task_reply=%#x task_data=%#x task_accepted=%u task_signal_seen=%u task_fence=%#x task_pending=%#x task_verified=%u task_retained=%u task_idle_notifications=%u task_idle_blocked=%u task_fence_value=%#x queue_poisoned=%u irq_enabled=%u irq_registered=%u irq_count=%u preempt_enabled=%u preempt_attempted=%u preempt_error=%d preempt_accepted=%u preempt_reply_valid=%u preempt_reply=%#x\n",
		!!boot->dla, !!boot->sroot, boot->upload_attempted,
		boot->upload_error, boot->upload_result.reply_valid,
		boot->upload_result.reply.header,
		boot->upload_result.idle_observed, !!boot->image,
		boot->ping.attempted, boot->ping.error, boot->ping.verified,
		boot->ping.word, !!boot->ping.buffer, boot->config.attempted,
		boot->config.error, boot->config.valid,
		boot->config.transaction.response_valid,
		boot->config.transaction.response.word, !!boot->config.buffer,
		boot->region5.attempted, boot->region5.error,
		boot->region5.registered, !!boot->region5.buffer,
		boot->config_apply.attempted, boot->config_apply.error,
		boot->config_apply.accepted,
		boot->config_apply.transaction.response_valid,
		boot->config_apply.transaction.response.word,
		boot->lpwr.attempted, boot->lpwr.error, boot->lpwr.accepted,
		boot->lpwr.transaction.response_valid,
		boot->lpwr.transaction.response.word, !!boot->lpwr.buffer,
		boot->task.attempted, boot->task.error,
		boot->task.response_valid, boot->task.response.word,
		boot->task.response.data, boot->task.accepted,
		boot->task.signal_seen, boot->task.fence, boot->task.pending,
		boot->task.verified, !!boot->task.buffer,
		boot->task.idle_notifications, boot->task.idle_blocked,
		boot->task.fence_value, boot->queue.poisoned, opt_enable_irq,
		boot->irq_registered, atomic_read(&boot->irq_count),
		enable_preempt, boot->preempt.attempted, boot->preempt.error,
		boot->preempt.accepted,
		boot->preempt.transaction.response_valid,
		boot->preempt.transaction.response.word);
	mutex_unlock(&boot->lock);
	return size;
}

/* Called with boot->lock held. This permits an attempt, not DMA reclamation. */
static bool n1x_boot_stop_ready(struct n1x_boot_context *boot)
{
	return boot->hfrp.powered && !boot->hfrp.error &&
	       boot->hfrp.channel.state == N1X_HFRP_READY &&
	       boot->hfrp.channel.pending_sequence == N1X_HFRP_NO_SEQUENCE &&
	       (boot->hfrp.gating.request_mask & 0x70) == 0x70 &&
	       !boot->queue.poisoned && !boot->queue.active &&
	       !boot->queue.owner_job &&
	       (!boot->upload_attempted ||
		(!boot->upload_error && boot->upload_result.reply_valid &&
		 boot->upload_result.idle_observed &&
		 !boot->upload_result.reply.status &&
		 !boot->upload_result.reply.detail)) &&
	       (!boot->ping.attempted || boot->ping.verified) &&
	       (!boot->config.attempted || boot->config.valid) &&
	       (!boot->region5.attempted || boot->region5.registered) &&
	       (!boot->config_apply.attempted || boot->config_apply.accepted) &&
	       (!boot->lpwr.attempted || boot->lpwr.accepted) &&
	       (!boot->task.attempted || boot->task.verified);
}

static void n1x_boot_free_buffer(struct n1x_boot_context *boot, void **buffer,
				 dma_addr_t *dma, size_t bytes)
{
	if (*buffer) {
		dma_free_coherent(boot->dla->dev, bytes, *buffer, *dma);
		*buffer = NULL;
		*dma = 0;
	}
}

/* accel->lock (if present) -> boot->lock, matching submission. Stop is terminal
 * for this module instance. A fresh load constructs a fresh channel/queue.
 * No speculative reset or rollback is attempted after any stop failure.
 */
static int n1x_boot_stop_hardware(struct device *dev)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err = 0;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (!boot->dla || boot->dla->dev != dev || !boot->sroot) {
		err = -ENODEV;
		goto out;
	}
	if (boot->shutdown.attempted) {
		err = boot->shutdown.error;
		goto out;
	}
	WRITE_ONCE(boot->stopping, true);
	boot->shutdown.attempted = true;
	boot->shutdown.stage = "preflight";
	if (!boot->hfrp.attempted && !boot->upload_attempted) {
		boot->shutdown.hardware_off =
			true; /* No write/DMA publisher existed. */
		goto done;
	}
	/* Shutdown requires a healthy, retired queue and complete setup. Retain
	 * mappings, PM and module references after any queue or setup failure.
	 */
	if (!n1x_boot_stop_ready(boot)) {
		err = -ENOTRECOVERABLE;
		goto done;
	}
	boot->shutdown.stage = "dma-domain";
	err = n1x_boot_dma_gate(boot->dla);
	if (!err)
		err = n1x_boot_dma_gate(boot->sroot);
	if (err)
		goto done;
	boot->shutdown.stage = "idle-drain";
	if (boot->task.attempted) {
		err = n1x_boot_drain_task_idle(boot, &boot->task);
		if (err)
			goto done;
	}
	/* Clear owned active interrupt mask bits and flush the write before
	 * power-off. Do NOT require every raw route/mask intersection bit to read
	 * back zero: N1X hardware retains bit 24 in both registers after this
	 * operation.
	 */
	boot->shutdown.stage = "irq-mask";
	err = n1x_boot_read(boot->dla, 0xd2c, &boot->shutdown.irq_owned);
	if (!err)
		err = n1x_boot_read(boot->dla, 0xd28,
				    &boot->shutdown.irq_mask_before);
	if (!err)
		err = n1x_boot_write(boot->dla, 0xd24,
				     boot->shutdown.irq_mask_before &
					     boot->shutdown.irq_owned);
	if (!err)
		err = n1x_boot_read(boot->dla, 0xd28,
				    &boot->shutdown.irq_mask_after);
	if (err)
		goto done;
	boot->shutdown.irq_readback_valid = true;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_hfrp_write,
				  n1x_boot_hfrp_read, n1x_boot_delay };
	boot->shutdown.stage = "hfrp-stop";
	err = n1x_hfrp_stop(&io, &boot->hfrp.channel, &boot->hfrp.version, 5001,
			    1000, &boot->shutdown.transport);
	if (err)
		goto done;
	if (!boot->shutdown.transport.confirmed) {
		err = -EIO;
		goto done;
	}
	boot->shutdown.hardware_off = true;
	WRITE_ONCE(boot->hfrp.powered, false);
	boot->shutdown.stage = "dma-release";
	/* SROOT's successful synchronous upload transaction was already consumed
	 * and firmware ran from its internal copy. Its failed/uncertain path is
	 * excluded above. DLA's continuing region/task DMA is now power gated.
	 */
	n1x_boot_free_buffer(boot, &boot->task.buffer, &boot->task.dma,
			     4096);
	n1x_boot_free_buffer(boot, &boot->lpwr.buffer, &boot->lpwr.dma,
			     4096);
	n1x_boot_free_buffer(boot, &boot->region5.buffer, &boot->region5.dma,
			     N1X_REGION5_ALLOC);
	n1x_boot_free_buffer(boot, &boot->config.buffer, &boot->config.dma,
			     4096);
	n1x_boot_free_buffer(boot, &boot->ping.buffer, &boot->ping.dma,
			     4096);
	if (boot->image) {
		dma_free_coherent(boot->sroot->dev, boot->image_bytes,
				  boot->image, boot->image_dma);
		boot->image = NULL;
		boot->image_dma = 0;
	}
done:
	boot->shutdown.error = err;
	dev_info(dev,
		 "shutdown stage=%s error=%d power_reply=%u state_valid=%u state=%#x off=%u irq_owned=%#x irq_before=%#x irq_after=%#x irq_readback=%u; %s\n",
		boot->shutdown.stage, err, boot->shutdown.transport.accepted,
		boot->shutdown.transport.state.valid,
		boot->shutdown.transport.state.raw, boot->shutdown.hardware_off,
		boot->shutdown.irq_owned, boot->shutdown.irq_mask_before,
		boot->shutdown.irq_mask_after,
		boot->shutdown.irq_readback_valid,
		err ? "DMA/lifetime retained, no retry" : "hardware stopped");
out:
	mutex_unlock(&boot->lock);
	return err;
}

/* The caller holds boot->lock. This authorizes retained system sleep only;
 * it never establishes DSP shutdown or permits reclaiming any DSP memory.
 */
static bool n1x_boot_clients_allow_sleep(struct n1x_boot_context *boot, bool require_votes)
{
	unsigned int i;

	if (boot->loader_poisoned || boot->client_leases != boot->retention_leases ||
	    boot->client_leases != boot->ready_clients ||
	    (require_votes && boot->sleeping_clients != boot->client_leases))
		return false;
	for (i = 0; i < ARRAY_SIZE(boot->dsp_buffers); i++) {
		struct n1x_sroot_client_buffer *buffer = &boot->dsp_buffers[i];

		if (buffer->published &&
		    (!boot->retention_leases || !buffer->sleep_retained ||
		     !buffer->staging_released || buffer->cpu))
			return false;
	}
	return true;
}

static int n1x_boot_stop_device(struct device *dev, bool for_sleep)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	unsigned int i;
	int err;

	if (!boot)
		return -ENODEV;
	if (!endpoint || endpoint->sroot)
		return -ENODEV;
	/* Legacy clients always block stop. Retention-aware clients permit only
	 * system sleep after every consumer acknowledged D3 and drained its work.
	 * Their DMA and provider lifetime remain owned across DLA stop/restart.
	 */
	mutex_lock(&boot->lock);
	if ((boot->client_leases &&
	     (!for_sleep || !n1x_boot_clients_allow_sleep(boot, true))) ||
	    (boot->sleep_prepared && !for_sleep)) {
		mutex_unlock(&boot->lock);
		return -EBUSY;
	}
	for (i = 0; i < ARRAY_SIZE(boot->dsp_buffers); i++) {
		if (boot->dsp_buffers[i].published &&
		    (!for_sleep || !n1x_boot_clients_allow_sleep(boot, true))) {
			mutex_unlock(&boot->lock);
			return -EBUSY;
		}
	}
	WRITE_ONCE(boot->stopping, true);
	mutex_unlock(&boot->lock);
	/* Keep the same lock order for shutdown and EXEC; never call this while
	 * holding boot->lock. accel_stop frees its list only after hardware success.
	 */
	err = endpoint->accel ? n1x_dla_accel_stop(endpoint->accel, n1x_boot_stop_hardware) :
					n1x_boot_stop_hardware(dev);
	if (err)
		return err;
	mutex_lock(&boot->lock);
	if (!boot->shutdown.released) {
		boot->shutdown.released = true;
		boot->shutdown.stage = "released";
		if (boot->upload_held) {
			pm_runtime_put_noidle(boot->sroot->dev);
			pm_runtime_put_noidle(boot->dla->dev);
			boot->upload_held = false;
			module_put(THIS_MODULE);
		}
		if (boot->hfrp_held) {
			pm_runtime_put_noidle(boot->dla->dev);
			boot->hfrp_held = false;
			module_put(THIS_MODULE);
		}
	}
	if (!boot->client_leases)
		n1x_boot_drop_publication(boot);
	mutex_unlock(&boot->lock);
	return 0;
}

static ssize_t shutdown_store(struct device *dev, struct device_attribute *attr,
					const char *buf, size_t count)
{
	int err;

	if (!capable(CAP_SYS_RAWIO) || !sysfs_streq(buf, "stop"))
		return -EPERM;
	err = n1x_boot_stop_device(dev, false);
	return err ? err : (ssize_t)count;
}
static DEVICE_ATTR_WO(shutdown);

static ssize_t shutdown_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	ssize_t size;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (!boot->dla || boot->dla->dev != dev) {
		mutex_unlock(&boot->lock);
		return -ENODEV;
	}
	size = sysfs_emit(buf,
			  "attempted=%u stage=%s error=%d accepted=%u state_valid=%u state=%#x hardware_off=%u released=%u hfrp_held=%u upload_held=%u irq_owned=%#x irq_before=%#x irq_after=%#x irq_readback=%u power_attempted=%u power_head_attempted=%u power_reply_valid=%u power_reply=%#x power_length=%u power_payload=%*phN query_head_attempted=%u query_reply_valid=%u query_reply=%#x query_length=%u query_payload=%*phN channel=%u pending=%u\n",
		boot->shutdown.attempted, boot->shutdown.stage ?: "not-started",
		boot->shutdown.error, boot->shutdown.transport.accepted,
		boot->shutdown.transport.state.valid,
		boot->shutdown.transport.state.raw, boot->shutdown.hardware_off,
		boot->shutdown.released, boot->hfrp_held, boot->upload_held,
		boot->shutdown.irq_owned, boot->shutdown.irq_mask_before,
		boot->shutdown.irq_mask_after,
		boot->shutdown.irq_readback_valid,
		boot->shutdown.transport.attempted,
		boot->shutdown.transport.power.tx.head_publish_attempted,
		boot->shutdown.transport.power.reply_valid,
		boot->shutdown.transport.power.reply.header,
		boot->shutdown.transport.power.reply.length,
		(int)boot->shutdown.transport.power.reply.length,
		boot->shutdown.transport.power.reply.payload,
		boot->shutdown.transport.query.tx.head_publish_attempted,
		boot->shutdown.transport.query.reply_valid,
		boot->shutdown.transport.query.reply.header,
		boot->shutdown.transport.query.reply.length,
		(int)boot->shutdown.transport.query.reply.length,
		boot->shutdown.transport.query.reply.payload,
		boot->hfrp.channel.state, boot->hfrp.channel.pending_sequence);
	mutex_unlock(&boot->lock);
	return size;
}

static int n1x_boot_start_once(struct n1x_boot_context *boot)
{
	int err;

	if (!boot->dla || !boot->dla->hfrp || !boot->sroot)
		return -ENODEV;
	if (boot->hfrp.attempted || boot->upload_attempted ||
	    boot->ping.attempted || boot->region5.attempted ||
	    boot->config.attempted || boot->config_apply.attempted ||
	    boot->lpwr.attempted || boot->task.attempted)
		return -EALREADY;
	err = n1x_boot_prepare_hfrp_once(boot);
	boot->hfrp.error = err;
	if (err)
		return err;
	err = n1x_boot_upload(boot);
	boot->upload_error = err;
	if (err)
		return err;
	err = n1x_boot_ping_once(boot);
	boot->ping.error = err;
	if (err)
		return err;
	err = n1x_boot_init_region5_once(boot);
	boot->region5.error = err;
	if (err)
		return err;
	err = n1x_boot_read_config_once(boot);
	boot->config.error = err;
	if (err)
		return err;
	err = n1x_boot_apply_config_once(boot);
	boot->config_apply.error = err;
	if (err)
		return err;
	err = n1x_boot_apply_lpwr_once(boot);
	boot->lpwr.error = err;
	if (err)
		return err;
	err = n1x_boot_bypass_task_once(boot);
	boot->task.error = err;
	return err;
}

static struct attribute *boot_attrs[] = {
	&dev_attr_shutdown.attr,
	&dev_attr_query_power_state.attr,
	NULL,
};

static umode_t boot_attr_is_visible(struct kobject *kobj, struct attribute *attr, int n)
{
	struct device *dev = kobj_to_dev(kobj);
	const struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);

	(void)n;
	if (!endpoint || endpoint->sroot)
		return 0;
	return attr->mode;
}

static const struct attribute_group boot_group = {
	.attrs = boot_attrs,
	.is_visible = boot_attr_is_visible,
};
__ATTRIBUTE_GROUPS(boot);

/* Enable HAL-1 preemption (method 0x31c, data=1 -> 0x1c02) while idle. */
static int n1x_boot_enable_preempt_once(struct n1x_boot_context *boot)
{
	struct n1x_dla_io io;
	u32 pending, attempt;
	int err;

	if (!enable_preempt)
		return -EPERM;
	if (!boot->dla || !boot->sroot || !boot->task.verified ||
	    !boot->lpwr.accepted || !boot->hfrp.powered ||
	    !boot->hfrp.policy.boot_tables_valid)
		return -ENODEV;
	if (boot->preempt.attempted)
		return boot->preempt.accepted ? 0 :
						(boot->preempt.error ?: -EIO);
	err = n1x_boot_drain_task_idle(boot, &boot->task);
	if (err)
		return err;
	err = n1x_boot_read(boot->dla, 8, &pending);
	if (err)
		return err;
	if (pending & 0xc0)
		return -EBUSY;
	io = (struct n1x_dla_io){ boot->dla, n1x_boot_write, n1x_boot_read,
				  n1x_boot_delay };
	boot->preempt.attempted = true;
	boot->preempt.transaction.method_attempted = 1;
	err = n1x_dla_send_method(&io, 0x31c, 1, 5001, 1000);
	if (err)
		goto out;
	for (attempt = 0; attempt < 5001; attempt++) {
		err = n1x_dla_take_response(&io, &boot->preempt.transaction.response);
		if (err != -EAGAIN)
			break;
		if (attempt + 1 < 5001)
			n1x_boot_delay(boot->dla, 1000);
	}
	if (err == -EAGAIN)
		err = -ETIMEDOUT;
	if (err)
		goto out;
	boot->preempt.transaction.response_valid = 1;
	if (boot->preempt.transaction.response.word != 0x1c02 ||
	    boot->preempt.transaction.response.data != 1) {
		err = -EPROTO;
		goto out;
	}
	boot->preempt.accepted = true;
out:
	boot->preempt.error = err;
	if (err)
		boot->queue.poisoned = true;
	return err;
}

static int n1x_boot_accel_ready(struct n1x_boot_context *boot,
				struct device *dev)
{
	if (boot->removing || boot->sleep_prepared)
		return -EBUSY;
	if (boot->stopping || !enable_preempt || !boot->dla ||
	    boot->dla->dev != dev || !boot->sroot || !boot->task.verified ||
	    !boot->lpwr.accepted || !boot->hfrp.powered ||
	    !boot->hfrp.policy.boot_tables_valid)
		return -ENODEV;
	return n1x_boot_dma_gate(boot->dla);
}

static int n1x_boot_accel_start(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_job_ops *ops)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	err = n1x_boot_accel_ready(boot, dev);
	if (!err && !boot->preempt.accepted)
		err = n1x_boot_enable_preempt_once(boot);
	if (!err)
		err = n1x_boot_drain_task_idle(boot, job);
	if (!err) {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_write,
					  n1x_boot_read, n1x_boot_delay };
		err = n1x_dla_job_start(&boot->queue, &io, ops, job);
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_boot_accel_poll(struct device *dev, struct n1x_dla_job *job,
					const struct n1x_dla_job_ops *ops)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	err = n1x_boot_accel_ready(boot, dev);
	if (!err) {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_write,
					  n1x_boot_read, n1x_boot_delay };
		err = n1x_dla_job_poll(&boot->queue, &io, ops, job);
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_boot_accel_preempt(struct device *dev, struct n1x_dla_job *job,
					const struct n1x_dla_preempt_ops *ops,
					u64 request_dma, u32 sequence,
					struct n1x_dla_preempt_result *result)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	err = n1x_boot_accel_ready(boot, dev);
	if (!err) {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_write,
					  n1x_boot_read, n1x_boot_delay };
		err = n1x_dla_job_preempt(&boot->queue, &io, ops, job,
					  request_dma, sequence, result);
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_boot_accel_resume(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_preempt_ops *ops)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	err = n1x_boot_accel_ready(boot, dev);
	if (!err) {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_write,
					  n1x_boot_read, n1x_boot_delay };
		err = n1x_dla_job_resume(&boot->queue, &io, ops, job);
	}
	mutex_unlock(&boot->lock);
	return err;
}

static int n1x_boot_accel_fail(struct device *dev, struct n1x_dla_job *job, int error)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (boot->removing || boot->sleep_prepared)
		err = -EBUSY;
	else if (!boot->dla || boot->dla->dev != dev)
		err = -ENODEV;
	else
		err = n1x_dla_job_fail(&boot->queue, job, error);
	mutex_unlock(&boot->lock);
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
static void n1x_boot_debugfs_add(struct n1x_boot_endpoint *endpoint);

/* accel->lock is outside boot->lock; sysfs never takes accel->lock. */
static int n1x_boot_accel_submit(struct device *dev, struct n1x_dla_job *job,
				const struct n1x_dla_job_ops *ops)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_dla_io io;
	int err;

	if (!boot)
		return -ENODEV;
	mutex_lock(&boot->lock);
	if (boot->removing || boot->sleep_prepared) {
		err = -EBUSY;
		goto out;
	}
	if (boot->stopping || !boot->dla || boot->dla->dev != dev ||
	    !boot->sroot || !boot->task.verified || !boot->lpwr.accepted ||
	    !boot->hfrp.powered || !boot->hfrp.policy.boot_tables_valid) {
		err = -ENODEV;
		goto out;
	}
	/* Successful startup already retained runtime references. */
	err = n1x_boot_dma_gate(boot->dla);
	if (!err)
		err = n1x_boot_drain_task_idle(boot, job);
	if (!err) {
		io = (struct n1x_dla_io){ boot->dla, n1x_boot_write,
					  n1x_boot_read, n1x_boot_delay };
		err = n1x_dla_job_execute(&boot->queue, &io, ops, job);
	}
out:
	mutex_unlock(&boot->lock);
	return err;
}

static struct acpi_device *n1x_find_sroot(struct acpi_device *consumer)
{
	struct acpi_device *candidate = NULL, *found = NULL;
	struct acpi_device *parent = acpi_dev_parent(consumer);

	if (!parent)
		return ERR_PTR(-ENODEV);
	for_each_acpi_dev_match(candidate, "NVDA8600", NULL, 1) {
		if (acpi_dev_parent(candidate) != parent)
			continue;
		if (found) {
			acpi_dev_put(found);
			acpi_dev_put(candidate);
			return ERR_PTR(-ENOTUNIQ);
		}
		found = acpi_dev_get(candidate);
	}
	return found ? found : ERR_PTR(-ENODEV);
}

static void n1x_match_put(struct device *dev, void *data)
{
	(void)dev;
	acpi_dev_put(data);
}

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

	if (!adev || (strcmp(acpi_device_hid(adev), "NVDA8600") &&
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
		resource_size(second) < (sroot ? 0x10 : 0x200))
		return -ENODEV;
	endpoint = devm_kzalloc(dev, sizeof(*endpoint), GFP_KERNEL);
	if (!endpoint)
		return -ENOMEM;
	endpoint->dev = dev;
	endpoint->sroot = sroot;
	if (sroot) {
		struct n1x_boot_context *boot = kzalloc_obj(*boot);

		if (!boot)
			return -ENOMEM;
		kref_init(&boot->refs);
		INIT_LIST_HEAD(&boot->entry);
		mutex_init(&boot->lock);
		init_waitqueue_head(&boot->waitq);
		atomic_set(&boot->irq_count, 0);
		boot->dev = get_device(dev);
		boot->sroot = endpoint;
		boot->scope = fwnode_get_parent(dev_fwnode(dev));
		endpoint->boot = boot;
		err = devm_add_action_or_reset(dev, n1x_boot_owner_put, boot);
		if (err)
			return err;
	}
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

			if (irq == -EPROBE_DEFER)
				return irq;
			if (irq > 0) {
				int irq_err = devm_request_irq(dev, irq, n1x_boot_irq_handler,
									IRQF_SHARED, dev_name(dev), endpoint);

				if (irq_err)
					dev_warn(dev, "IRQ %d request failed (%d); falling back to polling\n",
						irq, irq_err);
				else
					endpoint->irq = irq;
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
	platform_set_drvdata(pdev, endpoint);
	if (sroot) {
		struct n1x_boot_context *boot = endpoint->boot;

		mutex_lock(&n1x_contexts_lock);
		list_add_tail(&boot->entry, &n1x_contexts);
		boot->registered = true;
		mutex_unlock(&n1x_contexts_lock);
		err = devm_add_action_or_reset(dev, n1x_boot_unregister, boot);
		if (err)
			return err;
		n1x_boot_debugfs_add(endpoint);
		return component_add(dev, &n1x_sroot_component_ops);
	} else {
		struct component_match *match = NULL;
		struct acpi_device *supplier = n1x_find_sroot(adev);

		if (IS_ERR(supplier))
			return dev_err_probe(dev, PTR_ERR(supplier),
					     "no unique SROOT sibling in the firmware namespace\n");
		component_match_add_release(dev, &match, n1x_match_put,
					    n1x_compare_sroot, supplier);
		return component_master_add_with_match(dev, &n1x_master_ops,
						       match);
	}
}

static void n1x_boot_reset_state(struct n1x_boot_context *boot)
{
	if (!boot->shutdown.hardware_off || !boot->shutdown.released ||
	    boot->image || boot->ping.buffer || boot->config.buffer ||
	    boot->region5.buffer || boot->lpwr.buffer || boot->task.buffer)
		return;
	memset(&boot->upload_result, 0, sizeof(boot->upload_result));
	boot->upload_error = 0;
	boot->upload_attempted = false;
	WRITE_ONCE(boot->stopping, false);
	boot->hfrp_held = false;
	boot->upload_held = false;
	memset(&boot->shutdown, 0, sizeof(boot->shutdown));
	memset(&boot->hfrp, 0, sizeof(boot->hfrp));
	memset(&boot->ping, 0, sizeof(boot->ping));
	memset(&boot->config, 0, sizeof(boot->config));
	memset(&boot->region5, 0, sizeof(boot->region5));
	memset(&boot->config_apply, 0, sizeof(boot->config_apply));
	memset(&boot->lpwr, 0, sizeof(boot->lpwr));
	memset(&boot->task, 0, sizeof(boot->task));
	memset(&boot->preempt, 0, sizeof(boot->preempt));
	memset(&boot->queue, 0, sizeof(boot->queue));
}

static int n1x_sroot_component_bind(struct device *comp, struct device *master,
				    void *data)
{
	struct n1x_boot_endpoint *dla = data;
	struct n1x_boot_endpoint *sroot;
	struct n1x_boot_context *boot;
	struct device_link *link;

	if (!master->driver || comp->driver != master->driver)
		return -ENODEV;
	sroot = dev_get_drvdata(comp);
	if (!dla || dla->dev != master || dla->sroot || !sroot ||
	    sroot->dev != comp || !sroot->sroot || !sroot->boot)
		return -EINVAL;
	boot = sroot->boot;
	mutex_lock(&boot->lock);
	if (boot->removing || boot->hfrp.attempted || boot->upload_attempted ||
	    boot->client_leases) {
		mutex_unlock(&boot->lock);
		return -ENOTRECOVERABLE;
	}
	if (dla->sroot_link || (boot->dla && boot->dla != dla)) {
		mutex_unlock(&boot->lock);
		return -EBUSY;
	}
	mutex_unlock(&boot->lock);
	/* Presence is managed by components; this link orders system PM. */
	link = device_link_add(master, comp, DL_FLAG_STATELESS);
	if (!link)
		return -ENOMEM;
	mutex_lock(&boot->lock);
	boot->dla = dla;
	WRITE_ONCE(boot->stopping, false);
	WRITE_ONCE(dla->boot, boot);
	dla->sroot_link = link;
	boot->irq_registered = dla->irq > 0;
	mutex_lock(&n1x_contexts_lock);
	boot->consumer = get_device(master);
	mutex_unlock(&n1x_contexts_lock);
	mutex_unlock(&boot->lock);
	return 0;
}

static void n1x_sroot_component_unbind(struct device *comp, struct device *master,
				       void *data)
{
	struct n1x_boot_endpoint *dla = data;
	struct n1x_boot_context *boot = dla->boot;
	struct device_link *link;
	struct device *consumer;

	(void)comp;
	(void)master;
	if (!boot)
		return;
	mutex_lock(&boot->lock);
	WRITE_ONCE(boot->stopping, true);
	WRITE_ONCE(dla->boot, NULL);
	link = dla->sroot_link;
	dla->sroot_link = NULL;
	boot->dla = NULL;
	boot->irq_registered = false;
	mutex_lock(&n1x_contexts_lock);
	consumer = boot->consumer;
	boot->consumer = NULL;
	mutex_unlock(&n1x_contexts_lock);
	mutex_unlock(&boot->lock);
	/* IRQ callbacks borrow the endpoint's context until this drain completes. */
	if (dla->irq > 0)
		synchronize_irq(dla->irq);
	if (link)
		device_link_del(link);
	put_device(consumer);
}

static const struct component_ops n1x_sroot_component_ops = {
	.bind = n1x_sroot_component_bind,
	.unbind = n1x_sroot_component_unbind,
};

static int n1x_compare_sroot(struct device *dev, void *data)
{
	return ACPI_COMPANION(dev) == data;
}

static int n1x_master_bind(struct device *master)
{
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(master);
	struct n1x_boot_context *boot;
	struct n1x_boot_endpoint *dla_for_accel = NULL;
	int err;

	err = component_bind_all(master, endpoint);
	if (err)
		return err;
	boot = endpoint->boot;
	mutex_lock(&boot->lock);
	if (boot->dla && boot->sroot) {
		int start_err = 0;

		if (!boot->hfrp.attempted) {
			start_err = n1x_boot_start_once(boot);
			if (start_err) {
				dev_err(boot->dla->dev,
					"automatic startup failed (%d); device remains fail-closed\n",
					start_err);
			} else {
				/* Keep publication leases until explicit checked shutdown.
				 * remove() cannot abort module unloading if a later stop fails.
				 */
				dev_info(boot->dla->dev,
					 "N1X NPU initialized in-kernel (HFRP v%u.%u, %u kHz)\n",
					boot->hfrp.version.major,
					boot->hfrp.version.minor,
					boot->hfrp.policy.frequency_khz);
			}
		}
		if (!start_err && !boot->dla->accel)
			dla_for_accel = boot->dla;
	}
	mutex_unlock(&boot->lock);
	if (dla_for_accel) {
		struct n1x_dla_accel *accel;

		accel = n1x_dla_accel_register(dla_for_accel->dev, n1x_boot_accel_submit);
		if (IS_ERR(accel)) {
			/* Firmware already owns DMA. Returning a bind/probe failure
			 * would release devm mappings underneath that live firmware.
			 * Keep both components bound for checked shutdown and reload.
			 */
			dev_err(master,
				"accelerator registration failed (%ld); retaining bound hardware\n",
				PTR_ERR(accel));
			return 0;
		}
		if (enable_preempt)
			n1x_dla_accel_set_control(accel, &n1x_boot_control_ops);
		mutex_lock(&boot->lock);
		dla_for_accel->accel = accel;
		mutex_unlock(&boot->lock);
	}
	return 0;
}

static void n1x_master_unbind(struct device *master)
{
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(master);
	struct n1x_boot_context *boot = endpoint->boot;
	struct n1x_dla_accel *accel = NULL;
	bool do_stop = false;

	mutex_lock(&boot->lock);
	WRITE_ONCE(boot->stopping, true);
	if (boot->dla && boot->sroot &&
	    (boot->hfrp.attempted || boot->upload_attempted) &&
	    !boot->shutdown.attempted)
		do_stop = true;
	mutex_unlock(&boot->lock);
	if (do_stop) {
		int err = n1x_boot_stop_device(master, false);

		if (err)
			dev_err(master,
				"unbind shutdown failed (%d); retaining DMA, no forced reset\n",
				err);
	}
	mutex_lock(&boot->lock);
	if (boot->dla && boot->dla->accel) {
		accel = boot->dla->accel;
		boot->dla->accel = NULL;
	}
	mutex_unlock(&boot->lock);
	if (accel)
		n1x_dla_accel_unregister(accel);
	mutex_lock(&boot->lock);
	if (boot->shutdown.released) {
		n1x_boot_reset_state(boot);
		boot->suspended = false;
	}
	mutex_unlock(&boot->lock);
	component_unbind_all(master, endpoint);
}

static const struct component_master_ops n1x_master_ops = {
	.bind = n1x_master_bind,
	.unbind = n1x_master_unbind,
};

static void n1x_boot_remove(struct platform_device *pdev)
{
	struct n1x_boot_endpoint *endpoint = platform_get_drvdata(pdev);

	if (endpoint->sroot) {
		component_del(&pdev->dev, &n1x_sroot_component_ops);
		n1x_boot_unregister(endpoint->boot);
	} else {
		component_master_del(&pdev->dev, &n1x_master_ops);
	}
}

static int n1x_boot_prepare(struct device *dev)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	bool active_boot;
	int err = 0;

	if (!endpoint || endpoint->sroot)
		return 0;
	if (!boot)
		return 0;
	/* No device has suspended yet. Refuse conditions that cannot be resolved
	 * by our checked stop path, and prevent new public work until complete.
	 */
	mutex_lock(&boot->lock);
	active_boot = (boot->upload_attempted || boot->hfrp.attempted) &&
		      !boot->shutdown.released;
	if (!n1x_boot_clients_allow_sleep(boot, false) ||
	    (active_boot && (boot->stopping || !n1x_boot_stop_ready(boot)))) {
		err = -EBUSY;
		goto unlock;
	}
	boot->sleep_prepared = true;
	if (!++boot->sleep_generation)
		boot->sleep_generation++;
unlock:
	mutex_unlock(&boot->lock);
	return err;
}

static void n1x_boot_complete(struct device *dev)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);

	if (!boot)
		return;
	if (!endpoint || endpoint->sroot)
		return;
	mutex_lock(&boot->lock);
	boot->sleep_prepared = false;
	mutex_unlock(&boot->lock);
}

static int n1x_boot_suspend(struct device *dev)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	bool active_boot;
	int err = 0;

	if (!endpoint || endpoint->sroot)
		return 0;
	if (!boot)
		return 0;
	mutex_lock(&boot->lock);
	if (!n1x_boot_clients_allow_sleep(boot, true))
		err = -EBUSY;
	active_boot = (boot->upload_attempted || boot->hfrp.attempted) &&
		      !boot->shutdown.released;
	if (active_boot && (boot->queue.active || boot->queue.owner_job ||
			    boot->queue.poisoned))
		err = -EBUSY;
	mutex_unlock(&boot->lock);
	if (err || !active_boot)
		return err;
	err = n1x_boot_stop_device(dev, true);
	if (err)
		return err;
	mutex_lock(&boot->lock);
	n1x_boot_reset_state(boot);
	boot->suspended = true;
	mutex_unlock(&boot->lock);
	return 0;
}

static int n1x_boot_resume(struct device *dev)
{
	struct n1x_boot_context *boot __free(n1x_boot_put) = n1x_boot_get(dev);
	struct n1x_boot_endpoint *endpoint = dev_get_drvdata(dev);
	int err = 0;

	if (!endpoint || endpoint->sroot)
		return 0;
	if (!boot)
		return 0;
	mutex_lock(&boot->lock);
	if (boot->suspended) {
		err = boot->dla && boot->sroot ? n1x_boot_start_once(boot) : -ENODEV;
		if (!err)
			boot->suspended = false;
		if (err) {
			dev_err(dev,
				"resume startup failed (%d); device remains fail-closed\n",
				err);
		}
	}
	mutex_unlock(&boot->lock);
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
	err = n1x_boot_stop_device(&pdev->dev, false);
	if (err)
		dev_err(&pdev->dev, "shutdown failed (%d); retaining DMA, no forced reset\n", err);
}

static const struct dev_pm_ops n1x_boot_pm = {
	.prepare = pm_sleep_ptr(n1x_boot_prepare),
	.complete = pm_sleep_ptr(n1x_boot_complete),
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
		/* A failed hardware stop cannot be undone from remove(). */
		.suppress_bind_attrs = true,
	},
};

static struct dentry *boot_debugfs_dir;

static int n1x_debugfs_emit_attr(struct seq_file *m,
				 ssize_t (*show)(struct device *,
						 struct device_attribute *,
						 char *))
{
	struct n1x_boot_context *boot __free(n1x_boot_put) =
		m->private ? n1x_boot_get(m->private) : n1x_boot_get_unique();
	struct device *dev = NULL;
	ssize_t len;
	char *buf;

	if (IS_ERR_OR_NULL(boot))
		return boot ? PTR_ERR(boot) : -ENODEV;
	buf = kzalloc(PAGE_SIZE, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;
	mutex_lock(&boot->lock);
	if (boot->dla)
		dev = get_device(boot->dla->dev);
	mutex_unlock(&boot->lock);
	if (!dev) {
		kfree(buf);
		return -ENODEV;
	}
	len = show(dev, NULL, buf);
	put_device(dev);
	if (len > 0)
		seq_write(m, buf, len);
	kfree(buf);
	return len < 0 ? (int)len : 0;
}

/* Open files hold the device, not an unreferenced devres/boot pointer.
 * Reads look up a live context; debugfs removal rejects further operations.
 */
static int n1x_debugfs_open(struct inode *inode, struct file *file,
			    int (*show)(struct seq_file *, void *))
{
	struct device *dev = get_device(inode->i_private);
	int err = single_open(file, show, dev);

	if (err)
		put_device(dev);
	return err;
}

static int n1x_debugfs_release(struct inode *inode, struct file *file)
{
	struct seq_file *m = file->private_data;
	struct device *dev = m->private;
	int err = single_release(inode, file);

	put_device(dev);
	return err;
}

static int n1x_debugfs_status_show(struct seq_file *m, void *v)
{
	(void)v;
	return n1x_debugfs_emit_attr(m, status_show);
}

static int n1x_debugfs_status_open(struct inode *inode, struct file *file)
{
	return n1x_debugfs_open(inode, file, n1x_debugfs_status_show);
}

static const struct file_operations n1x_debugfs_status_fops = {
	.owner = THIS_MODULE,
	.open = n1x_debugfs_status_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = n1x_debugfs_release,
};

static int n1x_debugfs_hfrp_status_show(struct seq_file *m, void *v)
{
	(void)v;
	return n1x_debugfs_emit_attr(m, hfrp_status_show);
}

static int n1x_debugfs_hfrp_status_open(struct inode *inode, struct file *file)
{
	return n1x_debugfs_open(inode, file, n1x_debugfs_hfrp_status_show);
}

static const struct file_operations n1x_debugfs_hfrp_status_fops = {
	.owner = THIS_MODULE,
	.open = n1x_debugfs_hfrp_status_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = n1x_debugfs_release,
};

static int n1x_debugfs_shutdown_status_show(struct seq_file *m, void *v)
{
	(void)v;
	return n1x_debugfs_emit_attr(m, shutdown_status_show);
}

static int n1x_debugfs_shutdown_status_open(struct inode *inode,
					    struct file *file)
{
	return n1x_debugfs_open(inode, file, n1x_debugfs_shutdown_status_show);
}

static const struct file_operations n1x_debugfs_shutdown_status_fops = {
	.owner = THIS_MODULE,
	.open = n1x_debugfs_shutdown_status_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = n1x_debugfs_release,
};

static void n1x_boot_debugfs_remove(void *data)
{
	struct n1x_boot_endpoint *endpoint = data;

	debugfs_remove_recursive(endpoint->debugfs);
}

static void n1x_boot_debugfs_add(struct n1x_boot_endpoint *endpoint)
{
	struct dentry *dir;

	if (IS_ERR_OR_NULL(boot_debugfs_dir))
		return;
	dir = debugfs_create_dir(dev_name(endpoint->dev), boot_debugfs_dir);
	if (IS_ERR_OR_NULL(dir))
		return;
	endpoint->debugfs = dir;
	debugfs_create_file("status", 0400, dir, endpoint->dev,
			    &n1x_debugfs_status_fops);
	debugfs_create_file("hfrp_status", 0400, dir, endpoint->dev,
			    &n1x_debugfs_hfrp_status_fops);
	debugfs_create_file("shutdown_status", 0400, dir, endpoint->dev,
			    &n1x_debugfs_shutdown_status_fops);
	/* Remove before unregistering the context or releasing MMIO devres. */
	devm_add_action_or_reset(endpoint->dev, n1x_boot_debugfs_remove,
				 endpoint);
}

static int __init n1x_boot_init(void)
{
	int err;

	if (firmware_variant > 1)
		return -EINVAL;
	boot_debugfs_dir = debugfs_create_dir("n1x-npu-bootstrap", NULL);
	debugfs_create_file("status", 0400, boot_debugfs_dir, NULL,
			    &n1x_debugfs_status_fops);
	debugfs_create_file("hfrp_status", 0400, boot_debugfs_dir, NULL,
			    &n1x_debugfs_hfrp_status_fops);
	debugfs_create_file("shutdown_status", 0400, boot_debugfs_dir, NULL,
			    &n1x_debugfs_shutdown_status_fops);
	err = platform_driver_register(&n1x_boot_driver);
	if (err)
		debugfs_remove_recursive(boot_debugfs_dir);
	return err;
}
module_init(n1x_boot_init);
static void __exit n1x_boot_exit(void)
{
	platform_driver_unregister(&n1x_boot_driver);
	debugfs_remove_recursive(boot_debugfs_dir);
}
module_exit(n1x_boot_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("NVIDIA N1X (GB10/T264) NPU DRM compute accelerator driver");
MODULE_FIRMWARE(N1X_BOOT_FIRMWARE);
MODULE_FIRMWARE(N1X_BOOT_FALLBACK_FIRMWARE);
