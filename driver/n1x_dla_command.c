/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_command.h"

#define MTHDID 0x68
#define MTHDWDAT 0x6c
#define MTHD_PENDING (1U << 16)

int n1x_dla_send_method(const struct n1x_dla_io *io, __u32 method,
                       __u32 data, __u32 polls, __u32 interval_us)
{
    __u32 attempt, value;
    int err;
    if (!io || !io->write32 || !io->read32 || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)))
        return -EINVAL;
    for (attempt = 0; attempt < polls; attempt++) {
        err = io->read32(io->context, MTHDID, &value);
        if (err)
            return err;
        if (!(value & MTHD_PENDING))
            break;
        if (attempt + 1 == polls)
            return -ETIMEDOUT;
        io->delay_us(io->context, interval_us);
    }
    err = io->write32(io->context, MTHDWDAT, data);
    if (err)
        return err;
    return io->write32(io->context, MTHDID, method);
}

int n1x_dla_build_region(__u8 *out, size_t out_size, __u32 region,
                         __u64 address, __u32 bytes)
{
    __u8 record[16];
    unsigned int i;
    if (!out || out_size < sizeof(record) || (region != 1 && region != 3 && region != 5) ||
        !address || (address & 3) || !bytes || (region == 5 && bytes < 8) || address > ~(__u64)0 - bytes)
        return -EINVAL;
    /* Packed SET_REGIONS payload, verified against Windows d49c20,
     * T35x regions 1/3 and HAL-1 regions 1/3/5. The command points at this record, not at address.
     */
    for (i = 0; i < 4; i++) {
        record[i] = region >> (8 * i);
        record[12 + i] = bytes >> (8 * i);
    }
    for (i = 0; i < 8; i++)
        record[4 + i] = address >> (8 * i);
    for (i = 0; i < sizeof(record); i++)
        out[i] = record[i];
    return 0;
}

int n1x_dla_build_command23(__u8 *out, size_t out_size, __u8 context_47d,
                            __u32 context_490, __u32 provider_f4)
{
    __u8 record[40] = {1, context_47d != 0};
    __u32 scaled = context_490 ? provider_f4 * 2000U : 0;
    unsigned int i;

    if (!out || out_size < sizeof(record))
        return -EINVAL;
    for (i = 0; i < 4; i++)
        record[2 + i] = scaled >> (8 * i);
    for (i = 0; i < sizeof(record); i++)
        out[i] = record[i];
    return 0;
}

int n1x_dla_prepare_default_config(__u8 *buffer, size_t buffer_size)
{
    if (!buffer || buffer_size < N1X_DLA_CONFIG_BYTES)
        return -EINVAL;
    /* Windows d47830 with no effective +640 overrides and constructor
     * defaults +47e=1, +480=1. Field units/operating meanings stay opaque.
     */
    buffer[1] = 2;
    buffer[0x32] = 1;
    return 0;
}

int n1x_dla_build_preempt_request(__u8 *out, size_t out_size, __u64 dma,
                                  __u64 fence, __u32 submitted, __u32 sequence)
{
    __u8 request[N1X_DLA_PREEMPT_REQUEST_BYTES] = {0};
    __u64 post;
    unsigned int i;

    if (!out || out_size < sizeof(request) || !dma || (dma & 255) ||
        dma > ((__u64)1 << 36) - sizeof(request) || !fence || (fence & 3) ||
        fence > ((__u64)1 << 36) - 4 ||
        (fence < dma + sizeof(request) && fence + 4 > dma))
        return -EINVAL;
    post = dma + 256;
    request[0] = 1;
    /* Preserve Windows' sequence at packed +3 despite zero preaction length. */
    for (i = 0; i < 4; i++)
        request[3 + i] = sequence >> (8 * i);
    request[11] = 27;
    request[256] = request[269] = 0x80; /* WRITE_SEM */
    for (i = 0; i < 8; i++) {
        request[13 + i] = post >> (8 * i);
        request[257 + i] = fence >> (8 * i);
        request[270 + i] = fence >> (8 * i);
    }
    for (i = 0; i < 4; i++) {
        request[265 + i] = submitted >> (8 * i);
        request[278 + i] = sequence >> (8 * i);
    }
    /* Byte 282 is TERM. Both cache blocks belong to this allocation. */
    for (i = 0; i < sizeof(request); i++)
        out[i] = request[i];
    return 0;
}
