/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_task.h"

static void put16(__u8 *out, __u16 value)
{
    out[0] = value;
    out[1] = value >> 8;
}
static void put64(__u8 *out, __u64 value)
{
    unsigned int i;
    for (i = 0; i < 8; i++)
        out[i] = value >> (8 * i);
}
int n1x_dla_build_task_v2(__u8 *out, size_t out_size,
                         const struct n1x_dla_task_args *args)
{
    __u8 descriptor[N1X_DLA_TASK_V2_BYTES] = {0};
    unsigned int i;
    /* Copy parameters before writing so overlapping input/output is harmless. */
    if (!out || !args || out_size < sizeof(descriptor) ||
        (args->next & 255) || !args->address_list ||
        (args->address_list & 7) || !args->num_addresses)
        return -EINVAL;
    /* L4T R38.4 dla_task_descriptor: packed, aligned(4), version 2.
     * Actual t35x firmware independently verifies version at 8, engine at 9,
     * queue at 20 and next-pointer alignment, and copies a 60-byte header.
     */
    put64(descriptor, args->next);
    descriptor[8] = 2;
    descriptor[9] = 0x44;
    put16(descriptor + 10, sizeof(descriptor));
    put64(descriptor + 21, args->address_list);
    put16(descriptor + 29, args->num_addresses);
    put64(descriptor + 33, args->timeout);
    for (i = 0; i < sizeof(descriptor); i++)
        out[i] = descriptor[i];
    return 0;
}
