/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_core.h"

#define BOOT_VECTOR_LO 0xb80
#define BOOT_VECTOR_HI 0xb84
#define BCR_CTRL 0xe68
#define CPUCTL 0xb88
#define IDLESTATE 0x4c

int n1x_dla_select_riscv(const struct n1x_dla_io *io)
{
    __u32 value;
    int err;
    if (!io || !io->write32 || !io->read32)
        return -EINVAL;
    /* R38.4 rejects a nonzero high vector; the reconstructed Windows routine
     * does not check it. Refuse an unsupported address before any writes.
     */
    err = io->read32(io->context, BOOT_VECTOR_HI, &value);
    if (err)
        return err;
    if (value)
        return -EOPNOTSUPP;
    err = io->write32(io->context, BOOT_VECTOR_LO, 0x100000);
    if (err)
        return err;
    err = io->write32(io->context, BCR_CTRL, 0x10);
    if (err)
        return err;
    err = io->read32(io->context, BCR_CTRL, &value);
    if (err)
        return err;
    return (value & 0x11) == 0x11 ? 0 : -EIO;
}

int n1x_dla_start_core(const struct n1x_dla_io *io,
                      __u32 polls, __u32 interval_us)
{
    __u32 attempt, value;
    int err;
    if (!io || !io->write32 || !io->read32 || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)))
        return -EINVAL;
    err = io->write32(io->context, CPUCTL, 1);
    if (err)
        return err;
    for (attempt = 0; attempt < polls; attempt++) {
        err = io->read32(io->context, IDLESTATE, &value);
        if (err)
            return err;
        if (!value)
            return 0;
        if (attempt + 1 < polls)
            io->delay_us(io->context, interval_us);
    }
    return -ETIMEDOUT;
}
