/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_ACCEL_H
#define N1X_DLA_ACCEL_H
#include "n1x_dla_job.h"
struct device;
struct n1x_dla_accel;
/* Backend serializes all sysfs/accel work, checks startup/DMA/prerequisites,
 * drains idle notifications and executes the job. Any attempted job must pin
 * platform/module/power lifetime until checked device stop, even on error.
 */
typedef int (*n1x_dla_accel_submit)(struct device *, struct n1x_dla_job *,
                                  const struct n1x_dla_job_ops *);
struct n1x_dla_accel *n1x_dla_accel_register(struct device *, n1x_dla_accel_submit);
void n1x_dla_accel_unregister(struct n1x_dla_accel *);
/* Lock order: accel lock -> backend lock. Block future EXEC/REEXEC before
 * calling stop; retain all allocations on error. On success stop has proved
 * hardware gating and stopped every DMA publisher; free all retained jobs.
 * Does not invalidate live drm_file objects or their module references.
 */
int n1x_dla_accel_stop(struct n1x_dla_accel *, int (*stop)(struct device *));
#endif
