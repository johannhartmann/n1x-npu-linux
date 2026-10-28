/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_JOB_H
#define N1X_DLA_JOB_H
#include "n1x_dla_response.h"
#ifndef __KERNEL__
#include <stdbool.h>
#endif

/* Serialized queue, protected by the caller's device lock. Never reset this
 * structure while firmware from its generation may still access memory. */
struct n1x_dla_job_queue {
    __u32 last_fence;
    bool active, poisoned;
    struct n1x_dla_job *owner_job;
};
struct n1x_dla_job {
    void *buffer; /* Caller-owned backing; never allocated/freed by this helper. */
    __u64 dma;
    struct n1x_dla_response response;
    __u32 pending, fence;
    bool attempted, response_valid, accepted, signal_seen, verified;
    int error;
    __u32 idle_notifications;
    bool idle_blocked;
    __u32 fence_value;
    __u32 polls;
    /* Caller asserts the immutable descriptor has context+restore flags and
     * valid retained checkpoint/engine-context backing before first start. */
    bool resumable, suspended, preempt_completed;
    bool restore_pending; /* Resume submitted; saved marker may not be consumed yet. */
    __u32 preempt_sequence;
};
struct n1x_dla_job_ops {
    void *context;
    /* Both callbacks are synchronous: return zero or negative errno. */
    /* Write the unique terminal postaction value, initialize a different
     * coherent fence value and publish all prepared DMA backing. No device I/O. */
    int (*publish)(void *context, __u32 fence_value);
    /* Acquire device writes before reading the caller-owned fence. */
    int (*read_fence)(void *context, __u32 *value);
};
/* Execute one already prepared, fresh job on a caller-serialized queue.
 * Caller must validate/map the complete descriptor and model, drain stale
 * IRQs before preparation, and hold their backing throughout this call and
 * any later recovery. DMA descriptor address is nonzero, 256-byte aligned,
 * within 36 bits. Every job gets a non-reused 32-bit completion value; wrap is
 * refused. New job objects may be submitted after success. Any error after
 * method submission may have begun poisons the queue; caller must not reuse/free
 * backing or reset this queue to hide the failure. Success is the established
 * acceptance/SWGEN0/fence protocol, not proof of whole-device quiescence.
 * No allocation, unmapping, reset, IRQ registration or public userspace ABI.
 */
int n1x_dla_job_execute(struct n1x_dla_job_queue *queue,
                        const struct n1x_dla_io *io,
                        const struct n1x_dla_job_ops *ops,
                        struct n1x_dla_job *job);
/* Split form of execute, for a caller-owned serialized job lifecycle. Start
 * publishes once and sends once, reserving the queue for this exact job object.
 * Zero from start means command delivery, not acceptance or completion. Poll
 * processes at most one mailbox frame and returns -EINPROGRESS while pending.
 * It never sleeps; at most 5001 polls are allowed across all callers. The owner
 * must schedule polls with a bounded wall-clock deadline (execute uses 1ms)
 * and call fail on cancellation/deadline instead of silently abandoning the
 * active queue. No re-publication, concurrent consumer, job move/copy, or freeing
 * of job, queue, callback context or DMA backing while active is allowed.
 * Validation errors do not release another job's queue. Terminal execution
 * errors poison it; success releases ownership but does not prove safe DMA free.
 */
int n1x_dla_job_start(struct n1x_dla_job_queue *queue,
                      const struct n1x_dla_io *io,
                      const struct n1x_dla_job_ops *ops, struct n1x_dla_job *job);
int n1x_dla_job_poll(struct n1x_dla_job_queue *queue,
                     const struct n1x_dla_io *io,
                     const struct n1x_dla_job_ops *ops, struct n1x_dla_job *job);
/* Software quarantine only: never stops hardware or releases DMA. Negative
 * terminal errno required; -EINPROGRESS is not terminal. Caller holds lock. */
int n1x_dla_job_fail(struct n1x_dla_job_queue *queue, struct n1x_dla_job *job,
                     int error);
#endif
