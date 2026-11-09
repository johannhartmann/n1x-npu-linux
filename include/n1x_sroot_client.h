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
#endif
