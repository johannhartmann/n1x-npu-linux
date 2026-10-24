/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_registers.h"

struct register_range {
    __u32 logical, mapped, size;
};
/* Reconstructed factual ranges, NVIDIA 617.42 KMD RVA 0x14803d0/0x1480340. */
static const struct register_range mode1[] = {
    {0, 0x2000, 0x10}, {0x40, 0x2040, 0x10}, {0x64, 0x2064, 0x10},
    {0x80, 0x2080, 0x10}, {0x90, 0x2090, 4}, {0x3000, 0x2200, 4},
    {0xd20, 0x2320, 0x10}, {0x330, 0x2340, 0x10},
    {0xe5c, 0x2370, 4}, {0xe60, 0x2380, 0x10},
};
static const struct register_range mode2[] = {
    {0, 0x2000, 0x200}, {0x3000, 0x2200, 4}, {0x210, 0x2210, 0x10},
    {0xf20, 0x2220, 0x10}, {0x444, 0x2244, 4}, {0xb80, 0x2280, 0x10},
    {0xd20, 0x2320, 0x10}, {0x330, 0x2340, 0x10},
    {0xd60, 0x2360, 0x10}, {0xe5c, 0x2370, 4}, {0xe60, 0x2380, 0x20},
};

int n1x_dla_register_offset(enum n1x_dla_register_map map, __u32 logical,
                            size_t resource_size, __u32 *mapped)
{
    const struct register_range *ranges = 0;
    size_t count = 0, index;
    __u32 offset = logical;
    int found = 0;
    if (!mapped || (logical & 3))
        return -EINVAL;
    switch (map) {
    case N1X_DLA_MAP_DIRECT:
        found = 1;
        break;
    case N1X_DLA_MAP_WINDOWS_1:
        ranges = mode1;
        count = sizeof(mode1) / sizeof(mode1[0]);
        break;
    case N1X_DLA_MAP_WINDOWS_2:
        ranges = mode2;
        count = sizeof(mode2) / sizeof(mode2[0]);
        break;
    default:
        return -EINVAL;
    }
    for (index = 0; index < count; index++) {
        if (logical >= ranges[index].logical &&
            logical - ranges[index].logical < ranges[index].size) {
            offset = ranges[index].mapped + logical - ranges[index].logical;
            found = 1;
            break;
        }
    }
    if (!found)
        return -EINVAL;
    if (offset > resource_size || resource_size - offset < sizeof(__u32))
        return -ERANGE;
    *mapped = offset;
    return 0;
}
