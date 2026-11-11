/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_SROOT_CLIENT_H
#define N1X_SROOT_CLIENT_H
#include <linux/types.h>

/* Kernel-only ABI v1. The NVDA8600 owner serializes this with NPU upload.
 * No caller may independently map the SROOT MMIO aperture. The provider pins
 * its DMA allocation, runtime PM and module before the first mailbox write,
 * and keeps them until reboot even on failure. There is no retry/release API.
 * The client must separately retain its DSP memory and power references.
 */
struct n1x_sroot_client_result {
	__u32 reply_header;
	__u8 published, reply_valid, accepted, reserved;
};

/* Accepts MT8901 signed DSP containers with a supported image ID, without an
 * image-ID prefix. One attempt per image ID; no automatic retry.
 */
int n1x_sroot_upload_dsp_v1(__u32 image_id, const __u8 *image, size_t bytes,
			  struct n1x_sroot_client_result *result);

struct n1x_sroot_client_result_v2 {
	__u32 reply_header;
	__u8 published, reply_valid, accepted, staging_released;
};

/* ABI v2 releases only the temporary image allocation after the serialized
 * upload completes with the canonical successful reply. published records a
 * provider lifetime lease: PM/module references remain held and NPU shutdown
 * remains blocked even when staging_released is true. Uncertain or rejected
 * uploads retain their staging allocation too. No DSP memory is released;
 * there is no core shutdown, lease-release, retry or reinitialization API.
 */
int n1x_sroot_upload_dsp_v2(__u32 image_id, const __u8 *image, size_t bytes,
			  struct n1x_sroot_client_result_v2 *result);

struct device;
struct n1x_sroot_client;

/* ABI v3: acquire at consumer probe, using a unique firmware-parent match.
 * Keep a symbol_get(n1x_sroot_client_ops_v3) reference until after put().
 * get() returns ERR_PTR on failure and creates a DSP -> DLA -> SROOT PM link.
 * Serialize all operations on a handle, including put(), in the client.
 * prepare() reserves the provider BEFORE allocating/publishing DSP memory.
 * publish() makes that reservation irrevocable BEFORE the first DSP DMA
 * address write. cancel() unwinds only an unpublished reservation. put()
 * cancels and frees an unpublished handle; a published handle returns -EBUSY
 * and retains its context/device/module/link references until reboot.
 * upload() has v2 staging semantics and requires publish() first.
 */
struct n1x_sroot_client_ops {
	struct n1x_sroot_client *(*get)(struct device *consumer);
	int (*put)(struct n1x_sroot_client *client);
	int (*prepare)(struct n1x_sroot_client *client);
	int (*publish)(struct n1x_sroot_client *client);
	int (*cancel)(struct n1x_sroot_client *client);
	int (*upload)(struct n1x_sroot_client *client, __u32 image_id,
		      const __u8 *image, size_t bytes,
		      struct n1x_sroot_client_result_v2 *result);
};

const struct n1x_sroot_client_ops *n1x_sroot_client_ops_v3(void);

/* ABI v4 adds system-sleep retention, NOT lease release or core shutdown.
 * Use the returned client ops, including get(), for retention-aware handles.
 * All public work must be frozen by the consumer's PM prepare callback.
 * suspend() is called only AFTER acknowledged DSP D3, IRQ synchronization and
 * draining its power worker. resume() follows acknowledged D0. PM device links
 * order these votes around the provider's checked DLA stop/restart. No DMA or
 * provider/module/power references are released by either vote. Legacy handles,
 * uncertain uploads and missing votes still block the provider's system sleep.
 */
struct n1x_sroot_client_sleep_ops {
	const struct n1x_sroot_client_ops *client;
	int (*suspend)(struct n1x_sroot_client *client);
	int (*resume)(struct n1x_sroot_client *client);
};

const struct n1x_sroot_client_sleep_ops *n1x_sroot_client_ops_v4(void);

/* Additive typed resume operation; v1-v4 tables retain their layout/semantics.
 * After acknowledged DSP D3 and D0, restore its startup DRAM descriptor, call
 * this operation, then require a fresh firmware-ready notification before
 * releasing the v4 sleep vote. The provider serializes command 0x1a using the
 * first successfully uploaded image bound to this handle. No image/DMA buffer
 * is allocated or released. One attempt per provider PM generation; uncertain
 * completion poisons the loader. A rollback may call while PM is prepared even
 * if its sleep vote failed. This is not a general reset or live-recovery API.
 */
struct n1x_sroot_reinit_result {
	__u32 reply_header;
	__u8 attempted, reply_valid, accepted, reserved;
};

int n1x_sroot_reinit_dsp_v1(struct n1x_sroot_client *client,
			    struct n1x_sroot_reinit_result *result);

/* Opt-in lifetime operations for a consumer with a checked native power-off
 * path. Existing v1-v4 semantics remain unchanged. prepare() replaces the
 * ordinary prepare call, binds an exclusive image slot before DMA publication
 * and reports whether this provider observed a previous complete retirement.
 * That fact permits replacing stale startup descriptors, not live recovery.
 * retire() requires the consumer to freeze new work, detach all PCM users,
 * synchronize IRQ/work and complete the hardened platform D3/abandon path.
 * It releases only the provider lifetime reservation; the consumer still owns
 * its operating memory. Any uncertainty must retain every lease. A poisoned
 * loader cannot retire, even if the DSP itself was powered down.
 */
struct n1x_sroot_client_shutdown_ops {
	const struct n1x_sroot_client_sleep_ops *sleep;
	int (*prepare)(struct n1x_sroot_client *client, __u32 image_id,
		       __u8 *previously_stopped);
	/* Freeze this image's lifetime; primary HiFi5 waits for secondary HiFi5.
	 * On success no new dependent image may be prepared or uploaded.
	 * A later power failure retains this grant until reboot.
	 */
	int (*admit)(struct n1x_sroot_client *client);
	/* Record completed native D3/abandon; retain all references. retire()
	 * waits until every published DSP image has reached this barrier.
	 */
	int (*quiesced)(struct n1x_sroot_client *client);
	int (*retire)(struct n1x_sroot_client *client);
};

const struct n1x_sroot_client_shutdown_ops *n1x_sroot_client_ops_v5(void);
#endif
