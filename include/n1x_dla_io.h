/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_IO_H
#define N1X_DLA_IO_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

/* Register I/O callbacks for DLA, HFRP, and SROOT MMIO apertures. */
struct n1x_dla_io {
	void *context;
	int (*write32)(void *context, __u32 offset, __u32 value);
	int (*read32)(void *context, __u32 offset, __u32 *value);
	void (*delay_us)(void *context, __u32 interval_us);
};
#endif
