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
