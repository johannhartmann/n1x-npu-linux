/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_hfrp.h"

int n1x_hfrp_take_irq(const struct n1x_dla_io *io,
                     struct n1x_hfrp_irq_result *result)
{
    __u32 status;
    int err;

    if (!io || !io->read32 || !io->write32 || !result)
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    err = io->read32(io->context, 0x104, &status);
    if (err)
        return err;
    result->status = status;
    result->status_valid = 1;
    if (!status)
        return -EAGAIN;
    result->ack_attempted = 1;
    err = io->write32(io->context, 0x10c, status);
    if (err)
        return err;
    result->acknowledged = 1;
    return 0;
}

static int hfrp_read_byte(const struct n1x_dla_io *io, __u32 offset, __u8 *byte)
{
    __u32 word;
    int err = io->read32(io->context, offset & ~3U, &word);

    if (!err)
        *byte = (__u8)(word >> ((offset & 3U) * 8U));
    return err;
}

static int hfrp_write_byte(const struct n1x_dla_io *io, __u32 offset, __u8 byte)
{
    __u32 word, shift = (offset & 3U) * 8U;
    int err = io->read32(io->context, offset & ~3U, &word);

    if (err)
        return err;
    word = (word & ~(0xffU << shift)) | ((__u32)byte << shift);
    return io->write32(io->context, offset & ~3U, word);
}

int n1x_hfrp_queue_command(const struct n1x_dla_io *io, __u16 command,
                           __u16 sequence, const __u8 *payload, __u32 length,
                           struct n1x_hfrp_tx_result *result)
{
    __u32 client, server, head, tail, used, index, next;
    int err;

    if (!io || !io->read32 || !io->write32 || !result ||
        command > 0x3ffU || sequence >= N1X_HFRP_ASYNC_SEQ ||
        length > N1X_HFRP_RING_SIZE - 5U || (length && !payload))
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    err = io->read32(io->context, 0x110, &client);
    if (err)
        return err;
    err = io->read32(io->context, 0x188, &server);
    if (err)
        return err;
    head = client & 0xffU;
    tail = (server >> 8) & 0xffU;
    if (head >= N1X_HFRP_RING_SIZE || tail >= N1X_HFRP_RING_SIZE)
        return -EPROTO;
    used = (N1X_HFRP_RING_SIZE + head - tail) % N1X_HFRP_RING_SIZE;
    if (used + length + 4U >= N1X_HFRP_RING_SIZE)
        return -ENOSPC;
    result->header = (length + 4U) | ((__u32)sequence << 8) | ((__u32)command << 18);
    for (index = 0; index < length + 4U; ++index) {
        __u8 byte = index < 4U ? (__u8)(result->header >> (index * 8U)) : payload[index - 4U];

        err = hfrp_write_byte(io, 0x114 + head, byte);
        if (err)
            return err;
        head = (head + 1U) % N1X_HFRP_RING_SIZE;
    }
    /* Refresh so publishing the command head preserves the latest reply tail. */
    err = io->read32(io->context, 0x110, &client);
    if (err)
        return err;
    next = (client & ~0xffU) | head;
    result->head_publish_attempted = 1;
    err = io->write32(io->context, 0x110, next);
    if (err)
        return err;
    result->head_published = 1;
    result->doorbell_attempted = 1;
    err = io->write32(io->context, 0x100, 2);
    if (!err)
        result->doorbell_written = 1;
    return err;
}

int n1x_hfrp_read_reply(const struct n1x_dla_io *io,
                        struct n1x_hfrp_reply *reply)
{
    __u32 client, server, head, tail, available, index, total, header = 0;
    __u8 byte;
    int err;

    if (!io || !io->read32 || !io->write32 || !reply)
        return -EINVAL;
    memset(reply, 0, sizeof(*reply));
    err = io->read32(io->context, 0x188, &server);
    if (err)
        return err;
    err = io->read32(io->context, 0x110, &client);
    if (err)
        return err;
    head = server & 0xffU;
    tail = (client >> 8) & 0xffU;
    if (head >= N1X_HFRP_RING_SIZE || tail >= N1X_HFRP_RING_SIZE)
        return -EPROTO;
    available = (N1X_HFRP_RING_SIZE + head - tail) % N1X_HFRP_RING_SIZE;
    if (available < 4U)
        return -EAGAIN;
    for (index = 0; index < 4U; ++index) {
        err = hfrp_read_byte(io, 0x18c + tail, &byte);
        if (err)
            return err;
        header |= (__u32)byte << (index * 8U);
        tail = (tail + 1U) % N1X_HFRP_RING_SIZE;
    }
    total = header & 0xffU;
    if (total < 4U)
        return -EPROTO;
    if (total - 4U > N1X_HFRP_REPLY_MAX)
        return -EMSGSIZE;
    if (total > available)
        return -EAGAIN;
    reply->header = header;
    reply->sequence = (header >> 8) & 0x3ffU;
    reply->status = (header >> 18) & 0x3ffU;
    reply->length = total - 4U;
    for (index = 0; index < reply->length; ++index) {
        err = hfrp_read_byte(io, 0x18c + tail, &reply->payload[index]);
        if (err)
            return err;
        tail = (tail + 1U) % N1X_HFRP_RING_SIZE;
    }
    err = io->read32(io->context, 0x110, &client);
    if (err)
        return err;
    reply->tail_publish_attempted = 1;
    err = io->write32(io->context, 0x110, (client & ~0xff00U) | (tail << 8));
    if (!err)
        reply->tail_published = 1;
    return err;
}
