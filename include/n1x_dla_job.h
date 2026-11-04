/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_JOB_H
#define N1X_DLA_JOB_H
#include "n1x_dla_response.h"
#ifndef __KERNEL__
#include <stdbool.h>
#endif

struct n1x_dla_job_queue {
	__u32 last_fence;
	bool active, poisoned;
	struct n1x_dla_job *owner_job;
};

struct n1x_dla_job {
	void *buffer;
	__u64 dma;
	struct n1x_dla_response response;
	__u32 pending, fence;
	bool attempted, response_valid, accepted, signal_seen, verified;
	int error;
	__u32 idle_notifications;
	bool idle_blocked;
	__u32 fence_value;
	__u32 polls;
	bool resumable, suspended, preempt_completed;
	bool restore_pending;
	__u32 preempt_sequence;
};

struct n1x_dla_job_ops {
	void *context;
	int (*publish)(void *context, __u32 fence_value);
	int (*read_fence)(void *context, __u32 *value);
};

/* Execute a prepared task descriptor on a serialized queue and wait for completion. */
int n1x_dla_job_execute(struct n1x_dla_job_queue *queue,
			const struct n1x_dla_io *io,
			const struct n1x_dla_job_ops *ops,
			struct n1x_dla_job *job);

/* Step-wise start, non-blocking poll, and fail transitions for preemptible tasks. */
int n1x_dla_job_start(struct n1x_dla_job_queue *queue,
		      const struct n1x_dla_io *io,
		      const struct n1x_dla_job_ops *ops, struct n1x_dla_job *job);
int n1x_dla_job_poll(struct n1x_dla_job_queue *queue,
		     const struct n1x_dla_io *io,
		     const struct n1x_dla_job_ops *ops, struct n1x_dla_job *job);
int n1x_dla_job_fail(struct n1x_dla_job_queue *queue, struct n1x_dla_job *job,
		     int error);
#endif
