/* SPDX-License-Identifier: MIT */
#ifndef N1X_DLA_RUNTIME_H
#define N1X_DLA_RUNTIME_H
#include "uapi/n1x_dla_accel.h"
#ifdef __cplusplus
extern "C" {
#endif
/* All failures return negative errno. No automatic retries. Caller closes fd.
 * submit may return an execution error with a nonzero retained job_id. A syscall
 * failure may also happen after submission; never retry it automatically.
 */
int n1x_dla_open(const char *path);
int n1x_dla_info(int fd, struct n1x_dla_accel_info *info);
int n1x_dla_submit(int fd, const void *image, unsigned int bytes,
                   const struct n1x_dla_accel_range *ranges, unsigned int count,
                   struct n1x_dla_accel_exec *result);
int n1x_dla_read(int fd, unsigned long long job_id, unsigned long long offset,
                 void *output, unsigned long long bytes);
int n1x_dla_resubmit(int fd, unsigned long long job_id,
                     const struct n1x_dla_accel_patch *patches, unsigned int count,
                     struct n1x_dla_accel_exec *result);
#ifdef __cplusplus
}
#endif
#endif
