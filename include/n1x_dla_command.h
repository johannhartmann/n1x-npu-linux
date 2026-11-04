/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_COMMAND_H
#define N1X_DLA_COMMAND_H
#include "n1x_dla_io.h"

/* Submit a HAL-1 mailbox method after verifying the previous method has cleared. */
int n1x_dla_send_method(const struct n1x_dla_io *io, __u32 method,
			__u32 data, __u32 polls, __u32 interval_us);

/* Serialize a 16-byte SET_REGIONS record for printf (1), trace (3), or HAL-1 ring (5). */
int n1x_dla_build_region(__u8 *out, size_t out_size, __u32 region,
			 __u64 address, __u32 bytes);

/* Serialize the 40-byte HAL-1 LPWR command-23 payload (Windows KMD RVA 0xd45390). */
int n1x_dla_build_command23(__u8 *out, size_t out_size, __u8 context_47d,
			    __u32 context_490, __u32 provider_f4);

#define N1X_DLA_CONFIG_BYTES 252U
/* Update an exported 252-byte HAL-1 configuration snapshot in place for command 21. */
int n1x_dla_prepare_default_config(__u8 *buffer, size_t buffer_size);

#define N1X_DLA_PREEMPT_REQUEST_BYTES 512U
/* Serialize the 512-byte HAL-1 preemption request payload for method 0x212 (command 18). */
int n1x_dla_build_preempt_request(__u8 *out, size_t out_size, __u64 dma,
				  __u64 fence, __u32 submitted, __u32 sequence);
#endif
