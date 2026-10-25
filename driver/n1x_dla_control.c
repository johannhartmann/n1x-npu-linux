/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_dla_command.h"
#include "n1x_dla_control.h"

int n1x_dla_control_command(const struct n1x_dla_io *io, __u8 command,
                            __u64 dma_address, __u32 polls, __u32 interval_us,
                            struct n1x_dla_control_result *result)
{
    struct n1x_dla_response_fields fields;
    __u32 pending, attempt, data;
    int err;

    if (!io || !io->read32 || !io->write32 || !result || !polls || polls > 5001 ||
        (polls > 1 && (!interval_us || !io->delay_us)) ||
        (command != 11 && command != 21 && command != 22 && command != 23 && command != 24) ||
        !dma_address || (dma_address & 255) || dma_address > 0xffffffff00ULL)
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    err = io->read32(io->context, 8, &pending);
    if (err)
        return err;
    if (pending & 0x80)
        return -EBUSY;
    data = dma_address >> 8;
    result->method_attempted = 1;
    err = n1x_dla_send_method(io, 0x300 | command, data, polls, interval_us);
    if (err)
        return err;
    for (attempt = 0; attempt < polls; attempt++) {
        err = n1x_dla_take_response(io, &result->response);
        if (err != -EAGAIN)
            break;
        if (attempt + 1 < polls)
            io->delay_us(io->context, interval_us);
    }
    if (err == -EAGAIN)
        return -ETIMEDOUT;
    if (err)
        return err;
    result->response_valid = 1;
    err = n1x_dla_decode_response(&result->response, &fields);
    if (err)
        return err;
    if (fields.command != command || fields.data != data || fields.upper)
        return -EPROTO;
    if (fields.status)
        return -EREMOTEIO;
    return fields.message == 2 ? 0 : -EPROTO;
}
