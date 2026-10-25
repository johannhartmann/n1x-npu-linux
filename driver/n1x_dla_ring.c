/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_dla_ring.h"

static __u32 ring_le32(const __u8 *p)
{
    return (__u32)p[0] | (__u32)p[1] << 8 | (__u32)p[2] << 16 | (__u32)p[3] << 24;
}

int n1x_dla_region5_history(const __u8 *ring, size_t ring_size,
                           __u8 *output, size_t output_capacity, __u32 *count)
{
    __u32 first, second, capacity, entries, start, initial;
    size_t bytes;

    if (!ring || !count || ring_size < 8 + N1X_DLA_REGION5_HISTORY_MAX *
        N1X_DLA_REGION5_RECORD_BYTES || ring_size > ~(__u32)0 ||
        (ring_size - 8) % N1X_DLA_REGION5_RECORD_BYTES)
        return -EINVAL;
    first = ring_le32(ring);
    second = ring_le32(ring + 4);
    /* Original Windows allocation starts with a zero/zero host header.
     * Registration normalizes only the firmware-local copy to 8/8.
     */
    if (!first && !second) {
        *count = 0;
        return 0;
    }
    if (first < 8 || second < 8 || first > ring_size || second > ring_size ||
        (first - 8) % N1X_DLA_REGION5_RECORD_BYTES ||
        (second - 8) % N1X_DLA_REGION5_RECORD_BYTES)
        return -EINVAL;
    first = (first - 8) / N1X_DLA_REGION5_RECORD_BYTES;
    second = (second - 8) / N1X_DLA_REGION5_RECORD_BYTES;
    if (first == second) {
        *count = 0;
        return 0;
    }
    capacity = (ring_size - 8) / N1X_DLA_REGION5_RECORD_BYTES;
    entries = first ? N1X_DLA_REGION5_HISTORY_MAX : second;
    if (entries > N1X_DLA_REGION5_HISTORY_MAX)
        entries = N1X_DLA_REGION5_HISTORY_MAX;
    bytes = (size_t)entries * N1X_DLA_REGION5_RECORD_BYTES;
    if (!output || output_capacity < bytes)
        return -ENOSPC;
    start = (second + capacity - entries) % capacity;
    initial = capacity - start;
    if (initial > entries)
        initial = entries;
    memcpy(output, ring + 8 + (size_t)start * N1X_DLA_REGION5_RECORD_BYTES,
           (size_t)initial * N1X_DLA_REGION5_RECORD_BYTES);
    if (initial < entries)
        memcpy(output + (size_t)initial * N1X_DLA_REGION5_RECORD_BYTES,
               ring + 8, (size_t)(entries - initial) * N1X_DLA_REGION5_RECORD_BYTES);
    *count = entries;
    return 0;
}
