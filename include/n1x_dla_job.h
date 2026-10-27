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
};
struct n1x_dla_job_ops {
    void *context;
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
 * publication may have begun poisons the queue; caller must not reuse/free
 * backing or reset this queue to hide the failure. Success is the established
 * acceptance/SWGEN0/fence protocol, not proof of whole-device quiescence.
 * No allocation, unmapping, reset, IRQ registration or public userspace ABI.
 */
int n1x_dla_job_execute(struct n1x_dla_job_queue *queue,
                        const struct n1x_dla_io *io,
                        const struct n1x_dla_job_ops *ops,
                        struct n1x_dla_job *job);
#endif
