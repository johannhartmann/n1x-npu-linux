/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_STATISTICS_H
#define N1X_DLA_STATISTICS_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

struct n1x_dla_statistics {
    __u32 count;
    __u32 metadata_offset, metadata_stride;
    __u32 data_offset, data_stride;
};

/* Read-only validation of an immutable CPU snapshot. Supports only the
 * observed header words 0x00000300 / 0x00000002 and producer layout.
 * Metadata and per-record data remain opaque; no counter or timing decoding.
 * No DMA, device access, allocation, or output modification on failure.
 */
int n1x_dla_parse_statistics(const __u8 *data, size_t size,
                             struct n1x_dla_statistics *out);
#endif
