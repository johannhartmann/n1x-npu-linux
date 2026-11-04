/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_PREEMPT_H
#define N1X_DLA_PREEMPT_H
#include "n1x_dla_response.h"

enum n1x_dla_preempt_outcome {
	N1X_DLA_PREEMPT_UNRESOLVED,
	N1X_DLA_PREEMPT_CHECKPOINT,
	N1X_DLA_PREEMPT_TASK_FENCE,
};

struct n1x_dla_preempt_snapshot {
	__u64 checkpoint;
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

/* Issue HAL-1 preemption method 0x212 and wait for response 0x120b and fence completion. */
int n1x_dla_preempt(const struct n1x_dla_io *io,
		    const struct n1x_dla_preempt_ops *ops, __u64 dma,
		    __u32 task_fence, __u32 sequence,
		    struct n1x_dla_preempt_result *result);
#endif
