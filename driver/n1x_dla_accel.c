// SPDX-License-Identifier: GPL-2.0-only
/* Experimental trusted-model API. Submitted DMA is retained until reboot.
 * Kernel-produced descriptors contain no caller-supplied physical addresses.
 * Embedded model instructions are NOT sandboxed; every ioctl requires RAWIO.
 */
#include <linux/capability.h>
#include <linux/dma-mapping.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <drm/drm_accel.h>
#include <drm/drm_device.h>
#include <drm/drm_drv.h>
#include <drm/drm_ioctl.h>
#include <drm/drm_managed.h>
#include "n1x_dla_accel.h"
#include "n1x_dla_task.h"
#include "uapi/n1x_dla_accel.h"

#define N1X_JOB_PREFIX 8192U
#define N1X_JOB_FENCE 256U
#define N1X_JOB_ADDRESSES 512U
struct n1x_accel_job {
    struct list_head link;
    struct n1x_dla_job task;
    struct drm_file *owner; /* Cleared on close; never dereferenced. */
    u64 id;
    size_t allocated;
    u32 image_bytes;
};
struct n1x_dla_accel {
    struct drm_device drm;
    struct mutex lock;
    struct list_head jobs;
    n1x_dla_accel_submit submit;
    u64 last_id;
    size_t retained;
    u32 count;
    bool unplugged;
};
static struct n1x_dla_accel *to_accel(struct drm_device *drm)
{
    return container_of(drm, struct n1x_dla_accel, drm);
}
static int publish(void *context, u32 value)
{
    struct n1x_accel_job *job = context;
    u8 *p = job->task.buffer;
    unsigned int i;
    for (i = 0; i < 4; i++)
        p[73 + i] = value >> (i * 8);
    WRITE_ONCE(*(__le32 *)(p + N1X_JOB_FENCE), cpu_to_le32(~value));
    dma_wmb();
    return 0;
}
static int read_fence(void *context, u32 *value)
{
    struct n1x_accel_job *job = context;
    dma_rmb();
    *value = le32_to_cpu(READ_ONCE(*(__le32 *)(job->task.buffer + N1X_JOB_FENCE)));
    return 0;
}
static int info_ioctl(struct drm_device *drm, void *data, struct drm_file *file)
{
    struct n1x_dla_accel *accel = to_accel(drm);
    struct n1x_dla_accel_info *info = data;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    mutex_lock(&accel->lock);
    *info = (struct n1x_dla_accel_info){
        .abi_version = N1X_DLA_ABI_VERSION,
        .capabilities = N1X_DLA_CAP_RAWIO_REQUIRED | N1X_DLA_CAP_RETAIN_UNTIL_REBOOT,
        .max_image_bytes = N1X_DLA_MAX_IMAGE, .max_addresses = N1X_DLA_MAX_ADDRESSES,
        .retained_bytes = accel->retained, .retain_limit = N1X_DLA_RETAIN_LIMIT,
        .retained_jobs = accel->count, .max_jobs = N1X_DLA_MAX_JOBS,
    };
    mutex_unlock(&accel->lock);
    return 0;
}
static int exec_ioctl(struct drm_device *drm, void *data, struct drm_file *file)
{
    struct n1x_dla_accel *accel = to_accel(drm);
    struct n1x_dla_accel_exec *req = data;
    struct n1x_dla_accel_range *ranges;
    struct n1x_accel_job *job;
    struct n1x_dla_task_args args = {0};
    struct n1x_dla_job_ops ops;
    u64 *addresses = NULL;
    size_t bytes;
    unsigned int i;
    int err;

    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (req->flags || req->reserved || req->job_id || req->status || req->completed ||
        !req->image_ptr || !req->ranges_ptr || !req->image_bytes ||
        req->image_bytes > N1X_DLA_MAX_IMAGE || !req->num_addresses ||
        req->num_addresses > N1X_DLA_MAX_ADDRESSES)
        return -EINVAL;
    bytes = PAGE_ALIGN((size_t)req->image_bytes + N1X_JOB_PREFIX);
    ranges = memdup_user(u64_to_user_ptr(req->ranges_ptr),
                        req->num_addresses * sizeof(*ranges));
    if (IS_ERR(ranges))
        return PTR_ERR(ranges);
    for (i = 0; i < req->num_addresses; i++) {
        if (!ranges[i].bytes || ranges[i].offset >= req->image_bytes ||
            ranges[i].bytes > req->image_bytes - ranges[i].offset) {
            err = -EINVAL;
            goto free_ranges;
        }
    }
    addresses = kcalloc(req->num_addresses, sizeof(*addresses), GFP_KERNEL);
    if (!addresses) {
        err = -ENOMEM;
        goto free_ranges;
    }
    job = kzalloc(sizeof(*job), GFP_KERNEL);
    if (!job) {
        err = -ENOMEM;
        goto free_ranges;
    }
    mutex_lock(&accel->lock);
    if (accel->unplugged) {
        err = -ENODEV;
        goto free_job;
    }
    if (accel->count >= N1X_DLA_MAX_JOBS || bytes > N1X_DLA_RETAIN_LIMIT - accel->retained) {
        err = -ENOSPC;
        goto free_job;
    }
    if (accel->last_id == U64_MAX) {
        err = -EOVERFLOW;
        goto free_job;
    }
    job->allocated = bytes;
    job->image_bytes = req->image_bytes;
    job->task.buffer = dma_alloc_coherent(drm->dev, bytes, &job->task.dma, GFP_KERNEL);
    if (!job->task.buffer) {
        err = -ENOMEM;
        goto free_job;
    }
    if (!job->task.dma || (job->task.dma & 4095) ||
        job->task.dma > DMA_BIT_MASK(36) || bytes - 1 > DMA_BIT_MASK(36) - job->task.dma) {
        err = -ERANGE;
        goto free_dma;
    }
    memset(job->task.buffer, 0, bytes);
    if (copy_from_user(job->task.buffer + N1X_JOB_PREFIX,
                       u64_to_user_ptr(req->image_ptr), req->image_bytes)) {
        err = -EFAULT;
        goto free_dma;
    }
    for (i = 0; i < req->num_addresses; i++)
        addresses[i] = job->task.dma + N1X_JOB_PREFIX + ranges[i].offset;
    args.address_list = job->task.dma + N1X_JOB_ADDRESSES;
    args.num_addresses = req->num_addresses;
    args.timeout = 1000000;
    err = n1x_dla_build_address_list(job->task.buffer + N1X_JOB_ADDRESSES,
                                    N1X_JOB_PREFIX - N1X_JOB_ADDRESSES,
                                    addresses, req->num_addresses);
    if (!err)
        err = n1x_dla_build_fenced_task_v2(job->task.buffer, 256, &args,
                                          job->task.dma + N1X_JOB_FENCE, 0);
    if (err)
        goto free_dma;
    ops = (struct n1x_dla_job_ops){job, publish, read_fence};
    err = accel->submit(drm->dev, &job->task, &ops);
    if (!job->task.attempted) {
        if (!err)
            err = -EIO;
        goto free_dma;
    }
    /* The backend pins device/module lifetime before attempting submission.
     * Never free possibly active DMA on syscall failure, close, or job error.
     */
    job->id = ++accel->last_id;
    job->owner = file;
    list_add_tail(&job->link, &accel->jobs);
    accel->retained += bytes;
    accel->count++;
    req->job_id = job->id;
    req->status = job->task.error = err;
    req->completed = !err && job->task.verified;
    mutex_unlock(&accel->lock);
    kfree(addresses);
    kfree(ranges);
    return 0;
free_dma:
    dma_free_coherent(drm->dev, bytes, job->task.buffer, job->task.dma);
free_job:
    mutex_unlock(&accel->lock);
    kfree(job);
free_ranges:
    kfree(addresses);
    kfree(ranges);
    return err;
}
static int read_ioctl(struct drm_device *drm, void *data, struct drm_file *file)
{
    struct n1x_dla_accel *accel = to_accel(drm);
    const struct n1x_dla_accel_read *req = data;
    struct n1x_accel_job *job;
    int err = -ENOENT;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (req->reserved || !req->job_id || !req->data_ptr || !req->bytes)
        return -EINVAL;
    mutex_lock(&accel->lock);
    list_for_each_entry(job, &accel->jobs, link) {
        if (job->id != req->job_id || job->owner != file)
            continue;
        if (job->task.error || !job->task.verified) {
            err = -EIO;
            break;
        }
        if (req->offset >= job->image_bytes || req->bytes > job->image_bytes - req->offset) {
            err = -EINVAL;
            break;
        }
        dma_rmb();
        err = copy_to_user(u64_to_user_ptr(req->data_ptr),
                           job->task.buffer + N1X_JOB_PREFIX + req->offset,
                           req->bytes) ? -EFAULT : 0;
        break;
    }
    mutex_unlock(&accel->lock);
    return err;
}
static void postclose(struct drm_device *drm, struct drm_file *file)
{
    struct n1x_dla_accel *accel = to_accel(drm);
    struct n1x_accel_job *job;
    mutex_lock(&accel->lock);
    list_for_each_entry(job, &accel->jobs, link)
        if (job->owner == file)
            job->owner = NULL;
    mutex_unlock(&accel->lock);
}
/* No GEM mmap: all device-visible bytes are immutable after EXEC's snapshot. */
static const struct file_operations accel_fops = {
    .owner = THIS_MODULE, .open = accel_open, .release = drm_release,
    .unlocked_ioctl = drm_ioctl, .compat_ioctl = drm_compat_ioctl,
    .llseek = noop_llseek,
};
static const struct drm_ioctl_desc accel_ioctls[] = {
    DRM_IOCTL_DEF_DRV(N1X_DLA_INFO, info_ioctl, 0),
    DRM_IOCTL_DEF_DRV(N1X_DLA_EXEC, exec_ioctl, 0),
    DRM_IOCTL_DEF_DRV(N1X_DLA_READ, read_ioctl, 0),
};
static const struct drm_driver accel_driver = {
    .driver_features = DRIVER_COMPUTE_ACCEL,
    .fops = &accel_fops, .postclose = postclose,
    .ioctls = accel_ioctls, .num_ioctls = ARRAY_SIZE(accel_ioctls),
    .name = "n1x-dla", .desc = "Experimental N1X DLA", .major = 0, .minor = 1,
};
struct n1x_dla_accel *n1x_dla_accel_register(struct device *dev, n1x_dla_accel_submit submit)
{
    struct n1x_dla_accel *accel;
    int err;
    accel = devm_drm_dev_alloc(dev, &accel_driver, struct n1x_dla_accel, drm);
    if (IS_ERR(accel))
        return accel;
    err = drmm_mutex_init(&accel->drm, &accel->lock);
    if (err)
        return ERR_PTR(err);
    INIT_LIST_HEAD(&accel->jobs);
    accel->submit = submit;
    err = drm_dev_register(&accel->drm, 0);
    return err ? ERR_PTR(err) : accel;
}
void n1x_dla_accel_unregister(struct n1x_dla_accel *accel)
{
    mutex_lock(&accel->lock);
    accel->unplugged = true;
    mutex_unlock(&accel->lock);
    drm_dev_unplug(&accel->drm);
}
