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
