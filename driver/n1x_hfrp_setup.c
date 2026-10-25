/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#ifdef __KERNEL__
#include <linux/string.h>
#else
#include <string.h>
#endif
#include "n1x_hfrp_setup.h"

static void put16(__u8 *p, __u16 value)
{
    p[0] = value;
    p[1] = value >> 8;
}

static void put32(__u8 *p, __u32 value)
{
    put16(p, value);
    put16(p + 2, value >> 16);
}

static __u16 get16(const __u8 *p)
{
    return (__u16)p[0] | (__u16)p[1] << 8;
}

static int command(const struct n1x_dla_io *io,
                   struct n1x_hfrp_channel *channel, __u16 id,
                   const __u8 *payload, __u32 length, int reply_length,
                   __u32 polls, __u32 interval_us,
                   struct n1x_hfrp_setup_result *result)
{
    int err;

    result->command = id;
    err = n1x_hfrp_exchange(io, channel, id, payload, length, polls,
                             interval_us, &result->exchange);
    if (err)
        return err;
    if (reply_length >= 0 && result->exchange.reply.length != reply_length)
        return -EBADMSG;
    return 0;
}

int n1x_hfrp_configure_policy(const struct n1x_dla_io *io,
                               struct n1x_hfrp_channel *channel,
                               const struct n1x_hfrp_version *version,
                               const struct n1x_dla_vbios_policy *policy,
                               __u32 polls, __u32 interval_us,
                               struct n1x_hfrp_setup_result *result)
{
    struct n1x_dla_gating_policy gating;
    __u8 payload[36] = {2};
    const __u8 *reply;
    __u32 config_length, i, count;
    int err;

    if (!io || !io->read32 || !io->write32 || !channel || !version ||
        !version->valid || !result || !polls || polls > 5001 ||
        (polls > 1 && interval_us && !io->delay_us))
        return -EINVAL;
    if (version->major != 1 || (version->minor != 3 && version->minor < 5))
        return -EOPNOTSUPP;
    if (channel->state != N1X_HFRP_READY)
        return channel->state == N1X_HFRP_FAILED ? -ENOTRECOVERABLE : -EBUSY;
    if (channel->next_sequence >= N1X_HFRP_ASYNC_SEQ ||
        channel->pending_sequence != N1X_HFRP_NO_SEQUENCE)
        return -EINVAL;
    err = n1x_dla_prepare_gating(policy, &gating);
    if (err)
        return err;
    memset(result, 0, sizeof(*result));
    err = command(io, channel, 412, payload, 1, 1, polls, interval_us, result);
    if (err)
        goto failed;
    result->completed_commands++;

    memset(payload, 0, sizeof(payload));
    put16(payload, 4);
    put16(payload + 2, 10);
    put32(payload + 4, policy->entry_frequency_khz);
    put32(payload + 8, policy->exit_frequency_khz);
    put16(payload + 12, 1);
    config_length = version->minor == 3 ? 14 : 26;
    if (config_length == 26) {
        put16(payload + 18, 1);
        put16(payload + 20, gating.policy_bytes[2]);
        put16(payload + 22, gating.policy_bytes[5]);
        put16(payload + 24, gating.policy_bytes[8]);
    }
    err = command(io, channel, 401, payload, config_length, config_length,
                    polls, interval_us, result);
    if (err)
        goto failed;
    result->completed_commands++;

    err = command(io, channel, 428, NULL, 0, -1, polls, interval_us, result);
    if (err)
        goto failed;
    reply = result->exchange.reply.payload;
    if (result->exchange.reply.length < 2) {
        err = -EBADMSG;
        goto failed;
    }
    count = get16(reply);
    if (!count || count > 8 ||
        (result->exchange.reply.length != 2 + 4 * count &&
         result->exchange.reply.length != 34)) {
        err = -EBADMSG;
        goto failed;
    }
    for (i = 0; i < count; i++) {
        result->voltage_word[i] = get16(reply + 2 + 4 * i);
        result->frequency_khz[i] = (__u32)get16(reply + 4 + 4 * i) * 1000;
    }
    result->curve_count = count;
    result->completed_commands++;

    for (i = 0; i < 9; i++)
        put32(payload + 4 * i, policy->tuning[i]);
    err = command(io, channel, 408, payload, 36, 36, polls, interval_us, result);
    if (err)
        goto failed;
    result->completed_commands++;
    put32(payload, policy->power_cap_word);
    err = command(io, channel, 407, payload, 4, 4, polls, interval_us, result);
    if (err)
        goto failed;
    result->completed_commands++;
    if (version->minor >= 5) {
        for (i = 0; i < 3; i++) {
            put16(payload, i);
            payload[2] = 1;
            err = command(io, channel, 432, payload, 3, 26, polls, interval_us, result);
            if (err)
                goto failed;
            result->completed_commands++;
        }
    }
    result->configured = 1;
    return 0;
failed:
    channel->state = N1X_HFRP_FAILED;
    return err;
}
