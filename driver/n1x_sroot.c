/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include <linux/string.h>
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

int n1x_sroot_receive(const struct n1x_dla_io *io, __u32 stride,
                      struct n1x_sroot_reply *reply,
                      __u32 polls, __u32 interval_us)
{
    struct n1x_sroot_reply local = {0};
    __u32 i;
    int err;
    if (!io || !io->read32 || !io->write32 || !reply || !stride ||
        (stride & 3) || stride > ~(__u32)0 / 6 ||
        !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)))
        return -EINVAL;
    err = wait_ready(io, stride * 5, polls, interval_us);
    if (err)
        return err;
    err = io->read32(io->context, stride * 4, &local.header);
    if (err)
        return err;
    local.count = (local.header >> 8) & 15;
    if (local.count > N1X_SROOT_MAX_WORDS)
        return -EMSGSIZE;
    local.command = local.header & 255;
    local.status = (local.header >> 12) & 4095;
    local.detail = local.header >> 24;
    for (i = 0; i < local.count; i++) {
        err = io->read32(io->context, stride * 4, &local.words[i]);
        if (err)
            return err;
    }
    err = io->write32(io->context, stride * 6, 1);
    if (err)
        return err;
    *reply = local;
    return 0;
}

static int drain_pending(const struct n1x_dla_io *io, __u32 status_offset,
                         __u32 ack_offset, __u32 polls, __u32 interval_us)
{
    __u32 i, value;
    int err;
    for (i = 0; i < polls; i++) {
        err = io->read32(io->context, status_offset, &value);
        if (err)
            return err;
        if (!(value & 1))
            return 0;
        /* Keep the last observed pending item unacknowledged on timeout.
         * A successful acknowledgment must be followed by a clear observation.
         */
        if (i + 1 == polls)
            return -ETIMEDOUT;
        err = io->write32(io->context, ack_offset, 1);
        if (err)
            return err;
        io->delay_us(io->context, interval_us);
    }
    return -ETIMEDOUT;
}

int n1x_sroot_prepare(const struct n1x_dla_io *io, __u32 stride,
                      __u32 polls, __u32 interval_us)
{
    __u32 value;
    int err;
    if (!io || !io->read32 || !io->write32 || !stride || (stride & 3) ||
        stride > ~(__u32)0 / 12 || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)))
        return -EINVAL;
    err = io->write32(io->context, stride * 12, ~(__u32)0);
    if (err)
        return err;
    err = io->write32(io->context, stride * 3, 3);
    if (err)
        return err;
    err = drain_pending(io, stride * 5, stride * 6, polls, interval_us);
    if (err)
        return err;
    err = drain_pending(io, stride * 8, stride * 9, polls, interval_us);
    if (err)
        return err;
    err = wait_ready(io, stride * 2, polls, interval_us);
    if (err)
        return err;
    err = io->read32(io->context, stride * 11, &value);
    if (err)
        return err;
    return (value & 255) ? -EBUSY : 0;
}

int n1x_sroot_stage_image(__u8 *buffer, size_t capacity,
                          const __u8 *image, size_t size,
                          __u64 dma_address, __u32 words[3])
{
    __u32 tag;
    if (!buffer || !image || !words || size < 32 || capacity < size ||
        size > ~(__u32)0 || dma_address > ~(__u32)0 ||
        size - 1 > ~(__u32)0 - dma_address)
        return -EINVAL;
    tag = (__u32)image[0] | (__u32)image[1] << 8 |
          (__u32)image[2] << 16 | (__u32)image[3] << 24;
    if (tag != 0xc0300 && tag != 0x90400 &&
        (tag < 0xc0302 || tag > 0xc0305))
        return -EINVAL;
    memmove(buffer, image + 4, size - 4);
    /* Windows leaves these allocator-owned bytes untouched. Do not expose
     * unspecified allocation contents; the hardware meaning is still unknown.
     */
    memset(buffer + size - 4, 0, 4);
    words[0] = tag;
    words[1] = size;
    words[2] = dma_address;
    return 0;
}
