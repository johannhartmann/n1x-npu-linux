/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_statistics.h"

static __u32 get32(const __u8 *p)
{
    return (__u32)p[0] | (__u32)p[1] << 8 | (__u32)p[2] << 16 |
           (__u32)p[3] << 24;
}

int n1x_dla_parse_statistics(const __u8 *data, size_t size,
                             struct n1x_dla_statistics *out)
{
    struct n1x_dla_statistics parsed;
    __u64 metadata_end, data_end;
    __u32 i;

    if (!data || !out || size < 28 || size > (__u32)~0U)
        return -EINVAL;
    /* Original Windows compiler 11.4 builder RVA 0xc76488 writes these
     * exact eight bytes. This deliberately does not accept the older
     * runtime's statistics revision 1 by changing its version byte.
     */
    if (get32(data) != 0x300 || get32(data + 4) != 2)
        return -EINVAL;
    parsed.count = get32(data + 8);
    parsed.metadata_stride = get32(data + 12);
    parsed.metadata_offset = get32(data + 16);
    parsed.data_stride = get32(data + 20);
    parsed.data_offset = get32(data + 24);
    if (parsed.metadata_stride != 0x1800 || parsed.metadata_offset != 28 ||
        parsed.data_stride != 0x400 || (parsed.data_offset & 63))
        return -EINVAL;

    metadata_end = (__u64)parsed.metadata_offset +
                   (__u64)parsed.count * parsed.metadata_stride;
    data_end = (__u64)parsed.data_offset +
               (__u64)parsed.count * parsed.data_stride;
    if (metadata_end > parsed.data_offset || data_end > size)
        return -EINVAL;
    /* The producer reserves metadata using a separate active-graph count;
     * padding beyond round_up(28 + count * 0x1800, 64) is permitted.
     * Every copied record receives an absolute data offset at +0x1108,
     * incremented by 0x400 in serialized record order.
     */
    for (i = 0; i < parsed.count; i++) {
        size_t record = parsed.metadata_offset +
                        (size_t)i * parsed.metadata_stride;
        __u64 expected = (__u64)parsed.data_offset +
                         (__u64)i * parsed.data_stride;
        if (get32(data + record + 0x1108) != expected)
            return -EINVAL;
    }
    *out = parsed;
    return 0;
}
