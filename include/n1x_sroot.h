/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_SROOT_H
#define N1X_SROOT_H
#include "n1x_dla_io.h"

#define N1X_SROOT_MAX_WORDS 14
#define N1X_SROOT_CMD_UPLOAD 0x18

/* Send a command and payload words over the SROOT mailbox aperture. */
int n1x_sroot_send(const struct n1x_dla_io *io, __u32 stride,
		   __u8 command, const __u32 *words, __u8 count,
		   __u32 polls, __u32 interval_us);

struct n1x_sroot_reply {
	__u32 header;
	__u32 words[N1X_SROOT_MAX_WORDS];
	__u16 status;
	__u8 command, detail, count;
};

int n1x_sroot_receive(const struct n1x_dla_io *io, __u32 stride,
		      struct n1x_sroot_reply *reply,
		      __u32 polls, __u32 interval_us);

/* Drain stale reply/echo state on an idle SROOT mailbox before a new transaction. */
int n1x_sroot_prepare(const struct n1x_dla_io *io, __u32 stride,
		      __u32 polls, __u32 interval_us);

/* Stage a 0xc0302 SROOT firmware image into a 32-bit DMA window and build command 0x18 words. */
int n1x_sroot_stage_image(__u8 *buffer, size_t capacity,
			  const __u8 *image, size_t size,
			  __u64 dma_address, __u32 dma_minimum,
			  __u32 dma_maximum, __u32 words[3]);

/* Execute a serialized prepare -> TX -> RX SROOT mailbox transaction. */
int n1x_sroot_exchange(const struct n1x_dla_io *io, __u32 stride,
		       __u8 command, const __u32 *words, __u8 count,
		       struct n1x_sroot_reply *reply,
		       __u32 polls, __u32 interval_us);
#endif
