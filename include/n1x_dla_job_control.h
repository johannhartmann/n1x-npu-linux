/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_JOB_CONTROL_H
#define N1X_DLA_JOB_CONTROL_H
#include "n1x_dla_job.h"
#include "n1x_dla_preempt.h"

/* Private integration; no public cancellation ABI. Caller holds the same lock
 * across job and control operations. Only a job declared resumable before start
 * is eligible: immutable v2 descriptor flags 4|2, valid header/checkpoint and
 * compiler engine-context backing, preemption enabled while firmware was idle.
 * Caller publishes a valid request using a strictly increasing nonzero sequence
 * and a different submitted value, with a separate poisoned preemption fence.
 * Snapshot must refer to this job and that request. Preserve all backing.
 *
 * A checkpoint keeps queue ownership and blocks job_poll; job_resume resubmits
 * the same descriptor without publish(), changing inputs, resetting counters
 * or assigning another task fence. A task-fence outcome is a distinct protocol
 * completion (preempt_completed), possibly without SWGEN0. Neither outcome
 * proves physical engine quiescence or authorizes DMA reclamation. Caller must
 * enforce one absolute host deadline including time spent suspended; timeout
 * uses job_fail(), not retries or resetting the queue.
 *
 * EBUSY/EALREADY from preemption preflight with result.attempted==0 leave the
 * job running: poll its pending response/completion first. This includes a
 * known resume whose marker is still 1 after acceptance: firmware has not yet
 * consumed it. No request is sent or sequence consumed in that window. A
 * different nonzero marker or simultaneous task fence remains an error.
 * Other execution failures quarantine the queue. Invalid arguments/ownership
 * do no I/O.
 * Resume returns zero on submission, not acceptance or inference completion.
 */
int n1x_dla_job_preempt(struct n1x_dla_job_queue *queue,
                        const struct n1x_dla_io *io,
                        const struct n1x_dla_preempt_ops *ops,
                        struct n1x_dla_job *job, __u64 request_dma, __u32 sequence,
                        struct n1x_dla_preempt_result *result);
int n1x_dla_job_resume(struct n1x_dla_job_queue *queue,
                       const struct n1x_dla_io *io,
                       const struct n1x_dla_preempt_ops *ops, struct n1x_dla_job *job);
#endif
