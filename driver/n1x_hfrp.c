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
    if (used + length + 4U >= N1X_HFRP_RING_SIZE) {
        result->space_unavailable = 1;
        return -ENOSPC;
    }
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

static int hfrp_parameters(const struct n1x_dla_io *io,
                            const struct n1x_hfrp_channel *channel,
                            __u32 polls, __u32 interval_us,
                            const struct n1x_hfrp_exchange_result *result)
{
    if (!io || !io->read32 || !io->write32 || !channel || !result ||
        !polls || polls > 5001U ||
        (polls > 1U && interval_us && !io->delay_us))
        return -EINVAL;
    return 0;
}

static int hfrp_wait_bit(const struct n1x_dla_io *io, __u32 offset,
                         __u32 mask, __u32 expected, __u32 polls, __u32 interval)
{
    __u32 i, value;
    int err;

    for (i = 0; i < polls; ++i) {
        err = io->read32(io->context, offset, &value);
        if (err)
            return err;
        if ((value & mask) == expected)
            return 0;
        if (i + 1U < polls && interval)
            io->delay_us(io->context, interval);
    }
    return -ETIMEDOUT;
}

static int hfrp_reply_empty(const struct n1x_dla_io *io)
{
    __u32 client, server, head, tail;
    int err = io->read32(io->context, 0x188, &server);

    if (err)
        return err;
    err = io->read32(io->context, 0x110, &client);
    if (err)
        return err;
    head = server & 0xffU;
    tail = (client >> 8) & 0xffU;
    if (head >= N1X_HFRP_RING_SIZE || tail >= N1X_HFRP_RING_SIZE)
        return -EPROTO;
    return head == tail;
}

static int hfrp_collect(const struct n1x_dla_io *io,
                         struct n1x_hfrp_channel *channel,
                         struct n1x_hfrp_exchange_result *result, int startup)
{
    struct n1x_hfrp_reply reply;
    __u32 count;
    int err;

    /* At most 28 minimum-sized packets fit. Bound even a replenishing peer. */
    for (count = 0; count < N1X_HFRP_RING_SIZE / 4U; ++count) {
        err = n1x_hfrp_read_reply(io, &reply);
        if (err == -EAGAIN) {
            err = hfrp_reply_empty(io);
            if (err < 0)
                return err;
            if (err)
                return 0;
            /* A response may have arrived between snapshots. Recheck boundedly. */
            continue;
        }
        if (err) {
            result->reply = reply;
            result->reply_valid = 0;
            return err;
        }
        if (startup) {
            ++result->discarded_startup_replies;
        } else if (reply.sequence == N1X_HFRP_ASYNC_SEQ) {
            ++result->async_count;
            err = channel->async_reply ? channel->async_reply(channel->async_context, &reply) : -EOPNOTSUPP;
            if (err) {
                result->reply = reply;
                result->reply_valid = 0;
                return err;
            }
        } else if (reply.sequence != channel->pending_sequence || result->reply_valid) {
            result->reply = reply;
            result->reply_valid = 0;
            return -EPROTO;
        } else {
            result->reply = reply;
            result->reply_valid = 1;
        }
    }
    return -EOVERFLOW;
}

static int hfrp_drain_ack(const struct n1x_dla_io *io,
                           struct n1x_hfrp_channel *channel,
                           struct n1x_hfrp_exchange_result *result, int startup)
{
    int err;

    result->ack_attempted = 0;
    result->ack_completed = 0;
    err = hfrp_collect(io, channel, result, startup);

    if (err)
        return err;
    result->ack_attempted = 1;
    result->ack_completed = 0;
    err = io->write32(io->context, 0x10c, 2);
    if (err)
        return err;
    result->ack_completed = 1;
    /* Recheck after acknowledgment so a concurrent arrival is not overlooked. */
    return hfrp_collect(io, channel, result, startup);
}

static int hfrp_transact(const struct n1x_dla_io *io,
                          struct n1x_hfrp_channel *channel, __u16 command,
                          const __u8 *payload, __u32 length, __u32 polls,
                          __u32 interval, struct n1x_hfrp_exchange_result *result)
{
    __u32 i;
    int err;

    err = n1x_hfrp_queue_command(io, command, channel->next_sequence,
                                  payload, length, &result->tx);
    if (result->tx.head_publish_attempted) {
        channel->pending_sequence = channel->next_sequence;
        channel->next_sequence = (channel->next_sequence + 1U) % N1X_HFRP_ASYNC_SEQ;
    }
    if (err)
        return err;
    for (i = 0; i < polls; ++i) {
        err = hfrp_drain_ack(io, channel, result, 0);
        if (err)
            return err;
        if (result->reply_valid) {
            channel->pending_sequence = N1X_HFRP_NO_SEQUENCE;
            return result->reply.status ? -EREMOTEIO : 0;
        }
        if (i + 1U < polls && interval)
            io->delay_us(io->context, interval);
    }
    return -ETIMEDOUT;
}

int n1x_hfrp_handshake(const struct n1x_dla_io *io,
                        struct n1x_hfrp_channel *channel, __u32 polls,
                        __u32 interval_us, struct n1x_hfrp_exchange_result *result)
{
    static const __u8 payload[] = {0, 0, 0xcc, 0xdd, 0};
    int err = hfrp_parameters(io, channel, polls, interval_us, result);

    if (err)
        return err;
    if (channel->state != N1X_HFRP_NEW)
        return channel->state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EALREADY;
    memset(result, 0, sizeof(*result));
    channel->state = N1X_HFRP_ACTIVE;
    channel->next_sequence = 1;
    channel->pending_sequence = N1X_HFRP_NO_SEQUENCE;
    result->mode_select_attempted = 1;
    err = io->write32(io->context, 4, 1);
    if (err)
        goto failed;
    result->mode_selected = 1;
    result->reset_attempted = 1;
    err = io->write32(io->context, 0x100, 1);
    if (err)
        goto failed;
    err = hfrp_wait_bit(io, 0x100, 1, 0, polls, interval_us);
    if (err)
        goto failed;
    result->reset_completed = 1;
    err = hfrp_wait_bit(io, 0x104, 2, 2, polls, interval_us);
    if (err)
        goto failed;
    result->peer_ready = 1;
    err = hfrp_drain_ack(io, channel, result, 1);
    if (err)
        goto failed;
    err = hfrp_transact(io, channel, 1, payload, sizeof(payload), polls, interval_us, result);
    if (err)
        goto failed;
    channel->state = N1X_HFRP_READY;
    return 0;
failed:
    channel->state = N1X_HFRP_FAILED;
    return err;
}

int n1x_hfrp_discover_version(const struct n1x_dla_io *io,
                               struct n1x_hfrp_channel *channel, __u32 polls,
                               __u32 interval_us, struct n1x_hfrp_version *version,
                               struct n1x_hfrp_exchange_result *result)
{
    __u32 word;
    int err = hfrp_parameters(io, channel, polls, interval_us, result);

    if (err || !version)
        return -EINVAL;
    if (channel->state != N1X_HFRP_READY)
        return channel->state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EBUSY;
    if (channel->next_sequence >= N1X_HFRP_ASYNC_SEQ ||
        channel->pending_sequence != N1X_HFRP_NO_SEQUENCE)
        return -EINVAL;
    memset(version, 0, sizeof(*version));
    memset(result, 0, sizeof(*result));
    err = io->read32(io->context, 0, &word);
    if (err) {
        channel->state = N1X_HFRP_FAILED;
        return err;
    }
    version->stamp = word;
    version->stamp_valid = 1;
    if (word < 0x20250211U)
        return -EOPNOTSUPP;
    if (word < 0x2025ffffU) {
        version->major = 1;
        version->minor = word < 0x20250425U ? 3 : 5;
    } else {
        version->query_requested = 1;
        err = n1x_hfrp_exchange(io, channel, 999, NULL, 0, polls, interval_us, result);
        if (err)
            return err;
        if (result->reply.length != 4) {
            channel->state = N1X_HFRP_FAILED;
            return -EBADMSG;
        }
        word = (__u32)result->reply.payload[0] | ((__u32)result->reply.payload[1] << 8) |
               ((__u32)result->reply.payload[2] << 16) | ((__u32)result->reply.payload[3] << 24);
        version->response_word = word;
        version->major = (word >> 16) & 0xffU;
        version->minor = (word >> 8) & 0xffU;
    }
    version->valid = 1;
    return 0;
}

int n1x_hfrp_power_on(const struct n1x_dla_io *io,
                       struct n1x_hfrp_channel *channel,
                       const struct n1x_hfrp_version *version, __u8 pps,
                       __u32 polls, __u32 interval_us,
                       struct n1x_hfrp_exchange_result *result)
{
    __u8 payload[3] = {0x8c, 0, pps};

    if (!version || !version->valid)
        return -EINVAL;
    if (version->major != 1 || (version->minor != 3 && version->minor < 5))
        return -EOPNOTSUPP;
    return n1x_hfrp_exchange(io, channel, 400, payload,
                             version->minor == 3 ? 3 : 2, polls, interval_us, result);
}

int n1x_hfrp_query_power_state(const struct n1x_dla_io *io,
                                struct n1x_hfrp_channel *channel,
                                const struct n1x_hfrp_version *version,
                                __u32 polls, __u32 interval_us,
                                struct n1x_hfrp_power_state *state,
                                struct n1x_hfrp_exchange_result *result)
{
    int err = hfrp_parameters(io, channel, polls, interval_us, result);

    if (err || !version || !version->valid || !state)
        return -EINVAL;
    if (version->major != 1 || version->minor < 5)
        return -EOPNOTSUPP;
    if (channel->state != N1X_HFRP_READY)
        return channel->state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EBUSY;
    if (channel->next_sequence >= N1X_HFRP_ASYNC_SEQ ||
        channel->pending_sequence != N1X_HFRP_NO_SEQUENCE)
        return -EINVAL;
    memset(state, 0, sizeof(*state));
    err = n1x_hfrp_exchange(io, channel, 0x1af, NULL, 0, polls, interval_us, result);
    if (err)
        return err;
    if (result->reply.length != 1) {
        channel->state = N1X_HFRP_FAILED;
        return -EBADMSG;
    }
    state->raw = result->reply.payload[0];
    state->valid = 1;
    return 0;
}

int n1x_hfrp_exchange(const struct n1x_dla_io *io,
                       struct n1x_hfrp_channel *channel, __u16 command,
                       const __u8 *payload, __u32 length, __u32 polls,
                       __u32 interval_us, struct n1x_hfrp_exchange_result *result)
{
    int err = hfrp_parameters(io, channel, polls, interval_us, result);

    if (err || command > 0x3ffU || length > N1X_HFRP_RING_SIZE - 5U || (length && !payload))
        return -EINVAL;
    if (channel->state != N1X_HFRP_READY)
        return channel->state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EBUSY;
    if (channel->next_sequence >= N1X_HFRP_ASYNC_SEQ ||
        channel->pending_sequence != N1X_HFRP_NO_SEQUENCE)
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    channel->state = N1X_HFRP_ACTIVE;
    /* Reject stale synchronous replies before assigning a new sequence. */
    err = hfrp_drain_ack(io, channel, result, 0);
    if (err) {
        channel->state = N1X_HFRP_FAILED;
        return err;
    }
    err = hfrp_transact(io, channel, command, payload, length, polls, interval_us, result);
    channel->state = (!err || (err == -EREMOTEIO && result->reply_valid &&
                              result->reply.status && channel->pending_sequence == N1X_HFRP_NO_SEQUENCE) ||
                     (err == -ENOSPC && result->tx.space_unavailable))
                     ? N1X_HFRP_READY : N1X_HFRP_FAILED;
    return err;
}
