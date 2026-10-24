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
        (args->address_list & 255) || !args->num_addresses)
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

int n1x_dla_build_fenced_task_v2(__u8 *out, size_t out_size,
                                const struct n1x_dla_task_args *args,
                                __u64 fence_address, __u32 fence_value)
{
    __u8 descriptor[N1X_DLA_FENCED_TASK_V2_BYTES] = {0};
    unsigned int i;
    int err;

    if (!out || out_size < sizeof(descriptor) || !fence_address ||
        (fence_address & 3))
        return -EINVAL;
    err = n1x_dla_build_task_v2(descriptor, sizeof(descriptor), args);
    if (err)
        return err;
    put16(descriptor + 10, sizeof(descriptor));
    descriptor[15] = 1; /* One postaction list; no preactions. */
    put16(descriptor + 18, 60);
    put16(descriptor + 60, 64); /* List offset relative to descriptor base. */
    put16(descriptor + 62, 14); /* Opcode + u64 address + u32 value + TERM. */
    descriptor[64] = 0x80; /* WRITE_SEM */
    put64(descriptor + 65, fence_address);
    for (i = 0; i < 4; i++)
        descriptor[73 + i] = fence_value >> (8 * i);
    /* Byte 77 terminates the stream. Padding through byte 95 keeps the
     * interpreter's aligned 16-byte lookahead read inside the allocation.
     */
    for (i = 0; i < sizeof(descriptor); i++)
        out[i] = descriptor[i];
    return 0;
}

size_t n1x_dla_address_list_bytes(__u16 count)
{
    if (!count || count > 512)
        return 0;
    return ((size_t)count * 8 + 255) & ~(size_t)255;
}

int n1x_dla_build_address_list(__u8 *out, size_t out_size,
                              const __u64 *addresses, __u16 count)
{
    size_t bytes = n1x_dla_address_list_bytes(count);
    unsigned int i;
    if (!out || !addresses || !bytes || out_size < bytes)
        return -EINVAL;
    for (i = 0; i < count; i++)
        put64(out + i * 8, addresses[i]);
    for (i = count * 8; i < bytes; i++)
        out[i] = 0;
    return 0;
}
