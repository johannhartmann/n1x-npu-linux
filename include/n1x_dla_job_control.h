/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_JOB_CONTROL_H
#define N1X_DLA_JOB_CONTROL_H
#include "n1x_dla_job.h"
#include "n1x_dla_preempt.h"

/* Preempt an active resumable job (method 0x212) or resume a suspended checkpoint (method 0x307). */
int n1x_dla_job_preempt(struct n1x_dla_job_queue *queue,
			const struct n1x_dla_io *io,
			const struct n1x_dla_preempt_ops *ops,
			struct n1x_dla_job *job, __u64 request_dma, __u32 sequence,
			struct n1x_dla_preempt_result *result);
int n1x_dla_job_resume(struct n1x_dla_job_queue *queue,
		       const struct n1x_dla_io *io,
		       const struct n1x_dla_preempt_ops *ops, struct n1x_dla_job *job);
#endif
