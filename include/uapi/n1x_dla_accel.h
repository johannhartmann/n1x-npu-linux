/* SPDX-License-Identifier: MIT */
#ifndef N1X_DLA_ACCEL_UAPI_H
#define N1X_DLA_ACCEL_UAPI_H
#include <linux/types.h>
#ifdef __KERNEL__
#include <drm/drm.h>
#else
#include <libdrm/drm.h>
#endif
/* Experimental ABI. No compatibility with NVIDIA cuDLA is implied. */
#define N1X_DLA_ABI_VERSION 1
#define N1X_DLA_MAX_IMAGE (32U << 20)
#define N1X_DLA_RETAIN_LIMIT (64U << 20)
#define N1X_DLA_MAX_ADDRESSES 512U
#define N1X_DLA_MAX_JOBS 128U
#define N1X_DLA_CAP_RAWIO_REQUIRED 1U
#define N1X_DLA_CAP_RETAIN_UNTIL_REBOOT 2U
struct n1x_dla_accel_info {
    __u32 abi_version, capabilities;
    __u32 max_image_bytes, max_addresses;
    __u64 retained_bytes, retain_limit;
    __u32 retained_jobs, max_jobs;
    __u64 reserved;
};
/* Relative to the uploaded image, in the firmware task's address-index order.
 * These ranges do not validate the instruction stream inside that image. */
struct n1x_dla_accel_range { __u64 offset, bytes; };
struct n1x_dla_accel_exec {
    __u64 image_ptr, ranges_ptr;
    __u32 image_bytes, num_addresses;
    __u32 flags, reserved;
    /* Outputs: zero job_id means no submission. An attempted job is retained,
     * including after failure; status is its negative errno or zero.
     * Never retry EXEC automatically after a syscall error (including EFAULT).
     */
    __u64 job_id;
    __s32 status;
    __u32 completed;
};
struct n1x_dla_accel_read {
    __u64 job_id, offset, bytes, data_ptr;
    __u64 reserved;
};
#define DRM_N1X_DLA_INFO 0x00
#define DRM_N1X_DLA_EXEC 0x01
#define DRM_N1X_DLA_READ 0x02
#define DRM_IOCTL_N1X_DLA_INFO DRM_IOR(DRM_COMMAND_BASE + DRM_N1X_DLA_INFO, struct n1x_dla_accel_info)
#define DRM_IOCTL_N1X_DLA_EXEC DRM_IOWR(DRM_COMMAND_BASE + DRM_N1X_DLA_EXEC, struct n1x_dla_accel_exec)
#define DRM_IOCTL_N1X_DLA_READ DRM_IOW(DRM_COMMAND_BASE + DRM_N1X_DLA_READ, struct n1x_dla_accel_read)
#endif
