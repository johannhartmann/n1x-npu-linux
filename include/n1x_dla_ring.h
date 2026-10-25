/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_RING_H
#define N1X_DLA_RING_H
#include "n1x_dla_firmware.h"
#define N1X_DLA_REGION5_RECORD_BYTES 52U
#define N1X_DLA_REGION5_HISTORY_MAX 50U
/* Copy Windows' recent-history view from an IMMUTABLE CPU snapshot of region 5.
 * This is not a draining consumer and does not advance either header offset.
 * Caller must obtain a stable/coherent snapshot; do not use directly on a live
 * producer ring and infer atomicity, task completion or DMA quiescence.
 *
 * Header: zero/zero denotes the observed initial empty allocation; otherwise
 * two LE u32 byte offsets, each 8..ring_size and congruent to 8 mod 52.
 * ring_size includes the 8-byte header and must hold at least 50 records.
 * Windows snapshots min(second_index,50) when first_index=0, otherwise 50,
 * except equal indices return no records. Source order is the last N records
 * preceding the second cursor, wrapping at capacity. Records remain opaque.
 * Windows itself is less strict about invalid header positions; reject them.
 *
 * All objects (ring, output, count) must be separate, valid CPU memory. Source
 * extent is exactly ring_size; output capacity is bytes. Invalid input/short
 * output leaves output and count unchanged. An empty valid snapshot sets count
 * to zero without changing output and may use output=NULL, capacity=0.
 */
int n1x_dla_region5_history(const __u8 *ring, size_t ring_size,
                           __u8 *output, size_t output_capacity, __u32 *count);
#endif
