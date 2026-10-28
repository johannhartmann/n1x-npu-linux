/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_command.h"
#include "n1x_dla_preempt.h"

int n1x_dla_preempt(const struct n1x_dla_io *io,
                    const struct n1x_dla_preempt_ops *ops, __u64 dma,
                    __u32 task_fence, __u32 sequence,
                    struct n1x_dla_preempt_result *result)
{
    struct n1x_dla_response_fields fields;
    __u32 pending, attempt;
    int err;

    if (!result)
        return -EINVAL;
    *result = (struct n1x_dla_preempt_result){0};
    if (!io || !io->read32 || !io->write32 || !io->delay_us ||
        !ops || !ops->snapshot || !dma || (dma & 255) ||
        dma > ((__u64)1 << 36) - N1X_DLA_PREEMPT_REQUEST_BYTES || !task_fence)
        return -EINVAL;
    err = ops->snapshot(ops->context, &result->snapshot);
    if (err)
        return err;
    if (result->snapshot.checkpoint || result->snapshot.preempt_fence == sequence)
        return -ESTALE;
    if (result->snapshot.task_fence == task_fence)
        return -EALREADY;
    err = io->read32(io->context, 8, &pending);
    if (err)
        return err;
    if (pending & 0xc0)
        return -EBUSY;

    result->attempted = 1;
    err = n1x_dla_send_method(io, 0x212, dma >> 8, 5001, 1000);
    if (err)
        return err;
    for (attempt = 0; attempt < 5001; attempt++) {
        err = n1x_dla_take_response(io, &result->response);
        if (err && err != -EAGAIN)
            return err;
        if (!err) {
            result->response_valid = 1;
            if (result->response.word == 12 && !result->response.data) {
                result->idle_notifications++;
            } else {
                n1x_dla_decode_response(&result->response, &fields);
                if (fields.status || fields.message == 1 || fields.message == 8 ||
                    fields.message == 9 || fields.message == 10)
                    return -EREMOTEIO;
                if (result->response.word != 0x120b || result->response.data)
                    return -EPROTO;
                err = ops->snapshot(ops->context, &result->snapshot);
                if (err)
                    return err;
                if (result->snapshot.preempt_fence != sequence ||
                    result->snapshot.checkpoint > 1 ||
                    (result->snapshot.checkpoint && result->snapshot.task_fence == task_fence))
                    return -EPROTO;
                if (result->snapshot.checkpoint)
                    result->outcome = N1X_DLA_PREEMPT_CHECKPOINT;
                else if (result->snapshot.task_fence == task_fence)
                    result->outcome = N1X_DLA_PREEMPT_TASK_FENCE;
                else
                    return -EREMOTEIO; /* 0x120b alone also follows save failure. */
                return 0;
            }
        }
        if (attempt + 1 < 5001)
            io->delay_us(io->context, 1000);
    }
    return -ETIMEDOUT;
}
