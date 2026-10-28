/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_job_control.h"
#include "n1x_dla_command.h"

static int validate(struct n1x_dla_job_queue *queue, const struct n1x_dla_io *io,
                     const struct n1x_dla_preempt_ops *ops, struct n1x_dla_job *job)
{
    if (!queue || !job || !io || !io->read32 || !io->write32 || !io->delay_us ||
        !ops || !ops->snapshot)
        return -EINVAL;
    if (queue->poisoned)
        return -ENOTRECOVERABLE;
    if (!queue->active || queue->owner_job != job)
        return -ENOENT;
    if (!job->resumable)
        return -EOPNOTSUPP;
    if (!job->accepted || job->verified)
        return -EBUSY;
    return 0;
}

static int fail(struct n1x_dla_job_queue *queue, struct n1x_dla_job *job, int error)
{
    return n1x_dla_job_fail(queue, job, error == -EINPROGRESS ? -EIO : error);
}

/* Consume only exact idle messages after the control ACK. A late error must
 * not become successful completion or a resumable checkpoint. */
static int drain(const struct n1x_dla_io *io, struct n1x_dla_job *job)
{
    struct n1x_dla_response response;
    unsigned int i;
    int err;
    for (i = 0; i < 16; i++) {
        err = io->read32(io->context, 8, &job->pending);
        if (err)
            return err;
        if (!(job->pending & 0x80))
            return 0;
        err = n1x_dla_take_response(io, &response);
        if (err)
            return err;
        if (response.word != 12 || response.data) {
            job->response = response;
            job->response_valid = true;
            return -EREMOTEIO;
        }
        job->idle_notifications++;
    }
    return -ELOOP;
}

int n1x_dla_job_preempt(struct n1x_dla_job_queue *queue,
                        const struct n1x_dla_io *io,
                        const struct n1x_dla_preempt_ops *ops,
                        struct n1x_dla_job *job, __u64 request_dma, __u32 sequence,
                        struct n1x_dla_preempt_result *result)
{
    struct n1x_dla_preempt_snapshot snapshot;
    int err;
    if (!result)
        return -EINVAL;
    *result = (struct n1x_dla_preempt_result){0};
    err = validate(queue, io, ops, job);
    if (err)
        return err;
    if (job->suspended)
        return -EBUSY;
    if (!request_dma || (request_dma & 255) ||
        request_dma > ((__u64)1 << 36) - N1X_DLA_PREEMPT_REQUEST_BYTES ||
        !sequence || sequence <= job->preempt_sequence)
        return -EINVAL;
    if (job->restore_pending) {
        err = ops->snapshot(ops->context, &snapshot);
        if (err)
            return fail(queue, job, err);
        result->snapshot = snapshot;
        /* Acceptance ACK precedes restore-marker consumption. An untouched
         * marker in this known resume window is busy, not a failed context. */
        if (snapshot.checkpoint == 1 && snapshot.task_fence != job->fence_value)
            return -EBUSY;
        if (snapshot.checkpoint)
            return fail(queue, job, -ESTALE);
        job->restore_pending = false;
    }
    err = n1x_dla_preempt(io, ops, request_dma, job->fence_value, sequence, result);
    if (result->attempted)
        job->preempt_sequence = sequence;
    if (err) {
        if (!result->attempted && (err == -EBUSY || err == -EALREADY))
            return err;
        return fail(queue, job, err);
    }
    job->idle_notifications += result->idle_notifications;
    err = drain(io, job);
    if (err)
        return fail(queue, job, err);
    job->fence = result->snapshot.task_fence;
    if (result->outcome == N1X_DLA_PREEMPT_CHECKPOINT) {
        if (job->pending & 0x40)
            return fail(queue, job, -EPROTO);
        job->suspended = true;
        return 0;
    }
    if (result->outcome != N1X_DLA_PREEMPT_TASK_FENCE)
        return fail(queue, job, -EPROTO);
    if (job->pending & 0x40) {
        err = io->write32(io->context, 4, 0x40);
        if (err)
            return fail(queue, job, err);
        job->signal_seen = true;
    }
    job->preempt_completed = job->verified = true;
    job->error = 0;
    queue->active = false;
    queue->owner_job = 0;
    return 0;
}

int n1x_dla_job_resume(struct n1x_dla_job_queue *queue,
                       const struct n1x_dla_io *io,
                       const struct n1x_dla_preempt_ops *ops, struct n1x_dla_job *job)
{
    struct n1x_dla_preempt_snapshot snapshot;
    int err = validate(queue, io, ops, job);
    if (err)
        return err;
    if (!job->suspended)
        return -EINVAL;
    if (job->polls >= 5001)
        return fail(queue, job, -ETIMEDOUT);
    err = drain(io, job);
    if (!err && (job->pending & 0x40))
        err = -EPROTO;
    if (!err)
        err = ops->snapshot(ops->context, &snapshot);
    if (!err && (snapshot.checkpoint != 1 ||
                 snapshot.task_fence == job->fence_value ||
                 snapshot.preempt_fence != job->preempt_sequence))
        err = -ESTALE;
    if (err)
        return fail(queue, job, err);
    job->suspended = job->accepted = job->response_valid = job->signal_seen = false;
    job->restore_pending = true;
    job->response = (struct n1x_dla_response){0};
    /* Immutable descriptor already has restore permission. Firmware consumes
     * and clears the saved marker; the host must not clear it or republish. */
    err = n1x_dla_send_method(io, 0x307, job->dma >> 8, 5001, 1000);
    return err ? fail(queue, job, err) : 0;
}
