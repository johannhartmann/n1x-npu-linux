/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_sroot.h"

static int wait_ready(const struct n1x_dla_io *io, __u32 offset,
                       __u32 polls, __u32 interval_us)
{
    __u32 i, value;
    int err;
    for (i = 0; i < polls; i++) {
        err = io->read32(io->context, offset, &value);
        if (err)
            return err;
        if (value & 1)
            return 0;
        if (i + 1 < polls)
            io->delay_us(io->context, interval_us);
    }
    return -ETIMEDOUT;
}

int n1x_sroot_send(const struct n1x_dla_io *io, __u32 stride,
                   __u8 command, const __u32 *words, __u8 count,
                   __u32 polls, __u32 interval_us)
{
    __u32 header, value, i;
    int err, mismatch = 0;
    if (!io || !io->read32 || !io->write32 || !stride || (stride & 3) ||
        stride > ~(__u32)0 / 9 || count > N1X_SROOT_MAX_WORDS ||
        (count && !words) || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)))
        return -EINVAL;
    header = command | (__u32)count << 8;
    err = wait_ready(io, stride * 2, polls, interval_us);
    if (err)
        return err;
    err = io->write32(io->context, stride, header);
    if (err)
        return err;
    for (i = 0; i < count; i++) {
        err = io->write32(io->context, stride, words[i]);
        if (err)
            return err;
    }
    err = io->write32(io->context, stride * 3, 1);
    if (err)
        return err;
    err = wait_ready(io, stride * 8, polls, interval_us);
    if (err)
        return err;
    err = io->read32(io->context, stride * 7, &value);
    if (err)
        return err;
    if (~value != header)
        return -EIO;
    for (i = 0; i < count; i++) {
        err = io->read32(io->context, stride * 7, &value);
        if (err)
            return err;
        if (~value != words[i])
            mismatch = 1;
    }
    err = io->write32(io->context, stride * 9, 1);
    if (err)
        return err;
    return mismatch ? -EIO : 0;
}
