/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_response.h"

#define IRQSTAT 0x8
#define IRQMCLR 0xd24
#define IRQSCLR 0x4
#define MAILBOX0 0x40
#define MAILBOX1 0x44
#define SWGEN1 (1U << 7)

int n1x_dla_take_response(const struct n1x_dla_io *io,
                          struct n1x_dla_response *response)
{
    struct n1x_dla_response captured;
    __u32 status;
    int err;
    if (!io || !io->read32 || !io->write32 || !response)
        return -EINVAL;
    err = io->read32(io->context, IRQSTAT, &status);
    if (err)
        return err;
    if (!(status & SWGEN1))
        return -EAGAIN;
    err = io->read32(io->context, MAILBOX0, &captured.word);
    if (err)
        return err;
    err = io->read32(io->context, MAILBOX1, &captured.data);
    if (err)
        return err;
    /* L4T R38.4 ISR ordering; the actual t35x firmware waits for this ACK. */
    err = io->write32(io->context, IRQMCLR, SWGEN1);
    if (err)
        return err;
    err = io->write32(io->context, IRQSCLR, SWGEN1);
    if (err)
        return err;
    err = io->write32(io->context, MAILBOX0, N1X_DLA_RESPONSE_ACK);
    if (err)
        return err;
    *response = captured;
    return 0;
}
