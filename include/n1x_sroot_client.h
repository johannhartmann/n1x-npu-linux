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

/* Accepts only the three pinned ASUS MT8901 signed DSP images, without an
 * image-ID prefix. One attempt per image ID; no automatic retry.
 */
int n1x_sroot_upload_dsp_v1(__u32 image_id, const __u8 *image, size_t bytes,
			  struct n1x_sroot_client_result *result);
#endif
