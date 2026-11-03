/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_ACCEL_H
#define N1X_DLA_ACCEL_H
#include "n1x_dla_job.h"
#include "n1x_dla_preempt.h"
struct device;
struct n1x_dla_accel;
/* Backend serializes all sysfs/accel work, checks startup/DMA/prerequisites,
 * drains idle notifications and executes the job. Any attempted job must pin
 * platform/module/power lifetime until checked device stop, even on error.
 */
typedef int (*n1x_dla_accel_submit)(struct device *dev, struct n1x_dla_job *job,
				    const struct n1x_dla_job_ops *ops);
struct n1x_dla_accel_control_ops {
	int (*start)(struct device *dev, struct n1x_dla_job *job,
		     const struct n1x_dla_job_ops *ops);
	int (*poll)(struct device *dev, struct n1x_dla_job *job,
		    const struct n1x_dla_job_ops *ops);
	int (*preempt)(struct device *dev, struct n1x_dla_job *job,
		       const struct n1x_dla_preempt_ops *ops, __u64 request_dma,
		       __u32 sequence, struct n1x_dla_preempt_result *result);
	int (*resume)(struct device *dev, struct n1x_dla_job *job,
		      const struct n1x_dla_preempt_ops *ops);
	int (*fail)(struct device *dev, struct n1x_dla_job *job, int error);
};
struct n1x_dla_accel *n1x_dla_accel_register(struct device *dev,
					     n1x_dla_accel_submit submit);
void n1x_dla_accel_set_control(struct n1x_dla_accel *accel,
			       const struct n1x_dla_accel_control_ops *control);
void n1x_dla_accel_unregister(struct n1x_dla_accel *accel);
/* Lock order: accel lock -> backend lock. Block future EXEC/REEXEC before
 * calling stop; retain all allocations on error. On success stop has proved
 * hardware gating and stopped every DMA publisher; free all retained jobs.
 * Does not invalidate live drm_file objects or their module references.
 */
int n1x_dla_accel_stop(struct n1x_dla_accel *accel, int (*stop)(struct device *dev));
void n1x_dla_accel_resume(struct n1x_dla_accel *accel);
#endif
