/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_PREEMPT_H
#define N1X_DLA_PREEMPT_H
#include "n1x_dla_response.h"

/* Observations, not numerical correctness or DMA-quiescence guarantees. */
enum n1x_dla_preempt_outcome {
    N1X_DLA_PREEMPT_UNRESOLVED,
    N1X_DLA_PREEMPT_CHECKPOINT,
    N1X_DLA_PREEMPT_TASK_FENCE,
};
struct n1x_dla_preempt_snapshot {
    __u64 checkpoint; /* LE u64 checkpoint +24, acquired from coherent backing */
    __u32 task_fence, preempt_fence;
};
struct n1x_dla_preempt_ops {
    void *context;
    int (*snapshot)(void *context, struct n1x_dla_preempt_snapshot *snapshot);
};
struct n1x_dla_preempt_result {
    struct n1x_dla_response response;
    struct n1x_dla_preempt_snapshot snapshot;
    __u32 attempted, response_valid, idle_notifications;
    enum n1x_dla_preempt_outcome outcome;
};
/* Private HAL-1 control primitive; not called by EXEC. Caller owns the device
 * lock, an accepted context-enabled task and preemption enabled while idle.
 * No other task, method, IRQ consumer or resume may race this operation.
 * Caller publishes a build_preempt_request payload at dma with a fresh sequence,
 * distinct submitted/sequence values and a separate poisoned preemption fence.
 * All task/context/request allocations and their mappings remain caller-owned.
 * snapshot acquires DMA visibility and returns the separate fences and saved
 * marker. Require an initially zero marker and an unsignaled task fence.
 * No raw addresses from the snapshot are used and no payload is rewritten.
 *
 * Sends 0x212 exactly once. Each of method availability and completion waits
 * is bounded to 5001 polls/1000us intervals. Zero means an exact 0x120b/data=0
 * response, sequence fence and mutually exclusive checkpoint/task-fence evidence
 * were observed. Neither result allows freeing DMA, reusing buffers, delivering
 * inference output or resuming automatically. The caller must retain backing
 * and quarantine the queue after any attempted failure. ACK failure may have
 * consumed a response: no retry. Preflight failures do not ACK stale messages.
 * Result is initialized on entry; attempted includes ambiguous delivery errors.
 */
int n1x_dla_preempt(const struct n1x_dla_io *io,
                    const struct n1x_dla_preempt_ops *ops, __u64 dma,
                    __u32 task_fence, __u32 sequence,
                    struct n1x_dla_preempt_result *result);
#endif
