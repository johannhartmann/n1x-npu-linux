/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_job.h"
#include "n1x_dla_command.h"

static int execute(const struct n1x_dla_io *io,
                   const struct n1x_dla_job_ops *ops, struct n1x_dla_job *job)
{
    struct n1x_dla_response_fields fields;
    struct n1x_dla_response response;
    __u32 attempt;
    int err;
    job->attempted = true;
    err = n1x_dla_send_method(io, 0x307, job->dma >> 8, 5001, 1000);
    if (err)
        return err;
    for (attempt = 0; attempt < 5001; attempt++) {
        err = io->read32(io->context, 8, &job->pending);
        if (err)
            return err;
        if (job->pending & 0x80) {
            err = n1x_dla_take_response(io, &response);
            if (err)
                return err;
            if (response.word == 12 && response.data == 0) {
                job->idle_notifications++;
            } else {
                job->response = response;
                job->response_valid = true;
                err = n1x_dla_decode_response(&job->response, &fields);
                if (err)
                    return err;
                if (fields.command != 7 || fields.data != job->dma >> 8)
                    return -EPROTO;
                if (fields.status || fields.message == 1 || fields.message == 8 ||
                    fields.message == 9 || fields.message == 10)
                    return -EREMOTEIO;
                if (fields.upper || fields.message != 2 || job->accepted)
                    return -EPROTO;
                job->accepted = true;
            }
        }
        if (job->pending & 0x40)
            job->signal_seen = true;
        err = ops->read_fence(ops->context, &job->fence);
        if (err)
            return err;
        if (job->accepted && job->signal_seen && job->fence == job->fence_value) {
            /* A later error can coexist with a written fence. Recheck before
             * acknowledging SWGEN0; leave new mailbox work for the next poll.
             */
            err = io->read32(io->context, 8, &job->pending);
            if (err)
                return err;
            if (!(job->pending & 0x80)) {
                err = io->write32(io->context, 4, 0x40);
                if (err)
                    return err;
                job->verified = true;
                return 0;
            }
        }
        if (attempt + 1 < 5001)
            io->delay_us(io->context, 1000);
    }
    return -ETIMEDOUT;
}

int n1x_dla_job_execute(struct n1x_dla_job_queue *queue,
                        const struct n1x_dla_io *io,
                        const struct n1x_dla_job_ops *ops,
                        struct n1x_dla_job *job)
{
    int err;
    if (!queue || !io || !io->read32 || !io->write32 || !io->delay_us ||
        !ops || !ops->publish || !ops->read_fence || !job || !job->dma ||
        (job->dma & 255) || job->dma > (1ULL << 36) - 256)
        return -EINVAL;
    if (queue->poisoned || job->idle_blocked)
        return -ENOTRECOVERABLE;
    if (queue->active)
        return -EBUSY;
    if (job->attempted || job->fence_value || job->accepted || job->verified ||
        job->signal_seen || job->response_valid)
        return -EALREADY;
    if (queue->last_fence == (__u32)~0U)
        return -EOVERFLOW;
    queue->active = true;
    job->fence_value = ++queue->last_fence;
    err = ops->publish(ops->context, job->fence_value);
    if (!err) {
        err = ops->read_fence(ops->context, &job->fence);
        if (!err && job->fence == job->fence_value)
            err = -EINVAL; /* Publication must not pre-signal completion. */
    }
    if (!err)
        err = execute(io, ops, job);
    job->error = err;
    if (err && job->attempted)
        queue->poisoned = true;
    queue->active = false;
    return err;
}
