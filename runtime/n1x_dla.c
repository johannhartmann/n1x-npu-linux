// SPDX-License-Identifier: MIT
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#include "n1x_dla.h"
int n1x_dla_info(int fd, struct n1x_dla_accel_info *info)
{
    if (!info) return -EINVAL;
    memset(info, 0, sizeof(*info));
    return ioctl(fd, DRM_IOCTL_N1X_DLA_INFO, info) < 0 ? -errno : 0;
}
int n1x_dla_open(const char *path)
{
    char name[32] = {0};
    struct drm_version version = {.name_len = sizeof(name)-1, .name = name};
    struct n1x_dla_accel_info info;
    int err, fd;
    if (!path) return -EINVAL;
    fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) return -errno;
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) < 0) {
        err = -errno;
        goto fail;
    }
    if (version.name_len != strlen("n1x-dla") || strcmp(name, "n1x-dla")) {
        err = -ENODEV;
        goto fail;
    }
    err = n1x_dla_info(fd, &info);
    if (!err && info.abi_version != N1X_DLA_ABI_VERSION) err = -EPROTONOSUPPORT;
    if (err) goto fail;
    return fd;
fail:
    close(fd);
    return err;
}
int n1x_dla_submit_flags(int fd, const void *image, unsigned int bytes,
                         const struct n1x_dla_accel_range *ranges, unsigned int count,
                         const struct n1x_dla_accel_range *writable, unsigned int writable_count,
                         unsigned int flags, struct n1x_dla_accel_exec *result)
{
    if (!image || !ranges || !writable || !writable_count || !result ||
        (flags & ~N1X_DLA_EXEC_RESUMABLE)) return -EINVAL;
    *result = (struct n1x_dla_accel_exec){.image_ptr = (uintptr_t)image,
        .ranges_ptr = (uintptr_t)ranges, .writable_ranges_ptr = (uintptr_t)writable,
        .image_bytes = bytes, .num_addresses = count,
        .num_writable_ranges = writable_count, .flags = flags};
    if (ioctl(fd, DRM_IOCTL_N1X_DLA_EXEC, result) < 0) return -errno;
    if (!result->job_id || (!(flags & N1X_DLA_EXEC_RESUMABLE) && !result->status && !result->completed))
        return -EPROTO;
    return result->status;
}
int n1x_dla_submit(int fd, const void *image, unsigned int bytes,
                   const struct n1x_dla_accel_range *ranges, unsigned int count,
                   const struct n1x_dla_accel_range *writable, unsigned int writable_count,
                   struct n1x_dla_accel_exec *result)
{
    return n1x_dla_submit_flags(fd, image, bytes, ranges, count, writable, writable_count, 0, result);
}
int n1x_dla_read(int fd, unsigned long long job_id, unsigned long long offset,
                 void *output, unsigned long long bytes)
{
    struct n1x_dla_accel_read req = {.job_id = job_id, .offset = offset,
        .bytes = bytes, .data_ptr = (uintptr_t)output};
    if (!output || !bytes) return -EINVAL;
    return ioctl(fd, DRM_IOCTL_N1X_DLA_READ, &req) < 0 ? -errno : 0;
}
int n1x_dla_resubmit(int fd, unsigned long long job_id,
                     const struct n1x_dla_accel_patch *patches, unsigned int count,
                     struct n1x_dla_accel_exec *result)
{
    struct n1x_dla_accel_reexec req = {.job_id = job_id,
        .patches_ptr = (uintptr_t)patches, .num_patches = count};
    if (!job_id || !patches || !count || !result)
        return -EINVAL;
    memset(result, 0, sizeof(*result));
    if (ioctl(fd, DRM_IOCTL_N1X_DLA_REEXEC, &req) < 0)
        return -errno;
    result->job_id = req.new_job_id;
    result->status = req.status;
    result->completed = req.completed;
    if (!result->job_id || (!result->status && !result->completed))
        return -EPROTO;
    return result->status;
}
int n1x_dla_stats(int fd, unsigned long long job_id, unsigned long long offset,
                  unsigned long long bytes, struct n1x_dla_accel_stats *stats)
{
    if (!job_id || !bytes || !stats) return -EINVAL;
    *stats = (struct n1x_dla_accel_stats){.job_id = job_id, .offset = offset, .bytes = bytes};
    return ioctl(fd, DRM_IOCTL_N1X_DLA_STATS, stats) < 0 ? -errno : 0;
}
int n1x_dla_control(int fd, unsigned long long job_id, unsigned int action,
                    struct n1x_dla_accel_control *control)
{
    if (!job_id || !action || !control) return -EINVAL;
    *control = (struct n1x_dla_accel_control){.job_id = job_id, .action = action};
    if (ioctl(fd, DRM_IOCTL_N1X_DLA_CONTROL, control) < 0) return -errno;
    return control->status;
}
int n1x_dla_mmap(int fd, unsigned long long job_id, unsigned long long bytes,
                 const void **out_ptr)
{
    struct stat st;
    long page_size = sysconf(_SC_PAGESIZE);
    void *mapped;
    if (fd < 0 || !job_id || !bytes || !out_ptr || page_size <= 0)
        return -EINVAL;
    if (fstat(fd, &st) < 0)
        return -errno;
    if (S_ISCHR(st.st_mode) && major(st.st_rdev) == 1)
        return -ENODEV;
    mapped = mmap(NULL, (size_t)bytes, PROT_READ, MAP_SHARED, fd,
                  (off_t)(job_id * (unsigned long long)page_size));
    if (mapped == MAP_FAILED)
        return -errno;
    *out_ptr = mapped;
    return 0;
}
int n1x_dla_munmap(const void *ptr, unsigned long long bytes)
{
    if (!ptr || !bytes)
        return -EINVAL;
    return munmap((void *)ptr, (size_t)bytes) < 0 ? -errno : 0;
}
