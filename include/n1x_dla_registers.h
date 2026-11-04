/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_REGISTERS_H
#define N1X_DLA_REGISTERS_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

/* Register offset translation modes for direct vs. aperture-windowed MMIO. */
enum n1x_dla_register_map {
	N1X_DLA_MAP_DIRECT = 0,
	N1X_DLA_MAP_WINDOWS_1 = 1,
	N1X_DLA_MAP_WINDOWS_2 = 2,
};

/* Translate one aligned u32 register access and check resource bounds. */
int n1x_dla_register_offset(enum n1x_dla_register_map map, __u32 logical,
			    size_t resource_size, __u32 *mapped);
#endif
