/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_dla_boot.h"
#include "n1x_dla_core.h"

int n1x_dla_sroot_upload(const struct n1x_dla_io *sroot,
                         const struct n1x_dla_io *dla, __u32 stride,
                         const __u32 words[3], __u32 polls, __u32 interval_us,
                         struct n1x_dla_sroot_load_result *result)
{
    int err;

    /* Complete preflight before mailbox preparation can discard stale data. */
    if (!sroot || !sroot->read32 || !sroot->write32 || !dla || !dla->read32 ||
        !words || !result || !stride || (stride & 3) ||
        stride > ~(__u32)0 / 12 || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !sroot->delay_us || !dla->delay_us)))
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    result->upload_attempted = 1;
    err = n1x_sroot_exchange(sroot, stride, N1X_SROOT_CMD_UPLOAD, words, 3,
                             &result->reply, polls, interval_us);
    if (err)
        return err;
    result->reply_valid = 1;
    if (result->reply.status || result->reply.detail)
        return -EREMOTEIO;
    /* No CPUCTL write in the original method-2 path. */
    err = n1x_dla_wait_idle(dla, polls, interval_us);
    if (!err)
        result->idle_observed = 1;
    return err;
}
