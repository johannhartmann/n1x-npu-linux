/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_BOOT_H
#define N1X_DLA_BOOT_H
#include "n1x_sroot.h"

struct n1x_dla_sroot_load_result {
	struct n1x_sroot_reply reply;
	__u8 upload_attempted;
	__u8 reply_valid;
	__u8 idle_observed;
};

/* Execute the SROOT firmware upload exchange and wait for DLA IDLESTATE=0. */
int n1x_dla_sroot_upload(const struct n1x_dla_io *sroot,
			 const struct n1x_dla_io *dla, __u32 stride,
			 const __u32 words[3], __u32 polls, __u32 interval_us,
			 struct n1x_dla_sroot_load_result *result);
#endif
