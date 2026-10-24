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
