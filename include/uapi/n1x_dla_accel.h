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
#define N1X_DLA_ABI_VERSION 2
#define N1X_DLA_MAX_IMAGE (32U << 20)
#define N1X_DLA_RETAIN_LIMIT (64U << 20)
#define N1X_DLA_MAX_ADDRESSES 512U
#define N1X_DLA_MAX_WRITABLE_RANGES 64U
#define N1X_DLA_MAX_JOBS 128U
#define N1X_DLA_CAP_RAWIO_REQUIRED 1U
#define N1X_DLA_CAP_RETAIN_UNTIL_REBOOT 2U
#define N1X_DLA_CAP_REEXEC_IO 4U
/* With this bit, the privileged sysfs shutdown may invalidate all results and
 * release retained DMA after checked device stop. Close/unregister alone never
 * releases it. The retention flag keeps its historical name for compatibility.
 */
#define N1X_DLA_CAP_CONTROLLED_STOP 8U
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
    __u64 image_ptr, ranges_ptr, writable_ranges_ptr;
    __u32 image_bytes, num_addresses, num_writable_ranges, flags;
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
/* Opt-in reuse of a successfully completed, non-preemptible trusted task.
 * The kernel restricts patches to caller-registered writable ranges, intended
 * for model I/O. The CAP_SYS_RAWIO caller remains responsible for selecting
 * those ranges; the standard backend registers only tensor inputs/outputs.
 * Retains the allocation, including after error. Replaces the prior result ID.
 */
struct n1x_dla_accel_patch { __u64 offset, bytes, data_ptr; };
struct n1x_dla_accel_reexec {
    __u64 job_id, patches_ptr;
    __u32 num_patches, reserved;
    __u64 new_job_id;
    __s32 status;
    __u32 completed;
};
#define DRM_N1X_DLA_INFO 0x00
#define DRM_N1X_DLA_EXEC 0x01
#define DRM_N1X_DLA_READ 0x02
#define DRM_N1X_DLA_REEXEC 0x03
#define DRM_IOCTL_N1X_DLA_INFO DRM_IOR(DRM_COMMAND_BASE + DRM_N1X_DLA_INFO, struct n1x_dla_accel_info)
#define DRM_IOCTL_N1X_DLA_EXEC DRM_IOWR(DRM_COMMAND_BASE + DRM_N1X_DLA_EXEC, struct n1x_dla_accel_exec)
#define DRM_IOCTL_N1X_DLA_READ DRM_IOW(DRM_COMMAND_BASE + DRM_N1X_DLA_READ, struct n1x_dla_accel_read)
#define DRM_IOCTL_N1X_DLA_REEXEC DRM_IOWR(DRM_COMMAND_BASE + DRM_N1X_DLA_REEXEC, struct n1x_dla_accel_reexec)
#endif
