// SPDX-License-Identifier: GPL-2.0-only
/* Experimental trusted-model API. Submitted DMA is retained until checked stop.
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
    struct n1x_dla_accel_range *writable;
    u32 num_writable;
};
struct n1x_dla_accel {
    struct drm_device drm;
    struct mutex lock;
    struct list_head jobs;
    n1x_dla_accel_submit submit;
    u64 last_id;
    size_t retained;
    u32 count;
    bool unplugged, stopped;
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
    *value = le32_to_cpu(READ_ONCE(*(__le32 *)(job->task.buffer + N1X_JOB_FENCE)));
    /* Acquire result reads after the device's completion word. */
    dma_rmb();
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
        .capabilities = N1X_DLA_CAP_RAWIO_REQUIRED | N1X_DLA_CAP_RETAIN_UNTIL_REBOOT |
                        N1X_DLA_CAP_REEXEC_IO | N1X_DLA_CAP_CONTROLLED_STOP,
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
    struct n1x_dla_accel_range *ranges, *writable;
    struct n1x_accel_job *job;
    struct n1x_dla_task_args args = {0};
    struct n1x_dla_job_ops ops;
    u64 *addresses = NULL;
    size_t bytes;
    unsigned int i;
    int err;

    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (req->flags || req->job_id || req->status || req->completed ||
        !req->image_ptr || !req->ranges_ptr || !req->writable_ranges_ptr || !req->image_bytes ||
        req->image_bytes > N1X_DLA_MAX_IMAGE || !req->num_addresses ||
        req->num_addresses > N1X_DLA_MAX_ADDRESSES || !req->num_writable_ranges ||
        req->num_writable_ranges > N1X_DLA_MAX_WRITABLE_RANGES)
        return -EINVAL;
    bytes = PAGE_ALIGN((size_t)req->image_bytes + N1X_JOB_PREFIX);
    ranges = memdup_user(u64_to_user_ptr(req->ranges_ptr),
                        req->num_addresses * sizeof(*ranges));
    if (IS_ERR(ranges))
        return PTR_ERR(ranges);
    writable = memdup_user(u64_to_user_ptr(req->writable_ranges_ptr),
                           req->num_writable_ranges * sizeof(*writable));
    if (IS_ERR(writable)) {
        err = PTR_ERR(writable);
        writable = NULL;
        goto free_ranges;
    }
    for (i = 0; i < req->num_addresses; i++) {
        if (!ranges[i].bytes || ranges[i].offset >= req->image_bytes ||
            ranges[i].bytes > req->image_bytes - ranges[i].offset) {
            err = -EINVAL;
            goto free_ranges;
        }
    }
    for (i = 0; i < req->num_writable_ranges; i++) {
        unsigned int j;
        bool covered = false;
        if (!writable[i].bytes || writable[i].offset >= req->image_bytes ||
            writable[i].bytes > req->image_bytes - writable[i].offset) {
            err = -EINVAL;
            goto free_ranges;
        }
        for (j = 0; j < req->num_addresses; j++) {
            u64 delta;
            if (ranges[j].offset > writable[i].offset)
                continue;
            delta = writable[i].offset - ranges[j].offset;
            if (delta < ranges[j].bytes && writable[i].bytes <= ranges[j].bytes - delta) {
                covered = true;
                break;
            }
        }
        if (!covered) {
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
    if (accel->unplugged || accel->stopped) {
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
    job->writable = writable;
    job->num_writable = req->num_writable_ranges;
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
    writable = NULL;
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
    kfree(writable);
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
static int reexec_ioctl(struct drm_device *drm, void *data, struct drm_file *file)
{
    struct n1x_dla_accel *accel = to_accel(drm);
    struct n1x_dla_accel_reexec *req = data;
    struct n1x_dla_accel_patch *patches;
    struct n1x_accel_job *job = NULL, *candidate;
    struct n1x_dla_job_ops ops;
    void **copies;
    size_t total = 0;
    unsigned int i, j;
    int err = -ENOENT;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (!req->job_id || !req->patches_ptr || !req->num_patches || req->num_patches > 64 ||
        req->reserved || req->new_job_id || req->status || req->completed)
        return -EINVAL;
    patches = memdup_user(u64_to_user_ptr(req->patches_ptr), req->num_patches * sizeof(*patches));
    if (IS_ERR(patches))
        return PTR_ERR(patches);
    copies = kcalloc(req->num_patches, sizeof(*copies), GFP_KERNEL);
    if (!copies) {
        kfree(patches);
        return -ENOMEM;
    }
    /* Snapshot every user byte before modifying any device-visible storage. */
    for (i = 0; i < req->num_patches; i++) {
        if (!patches[i].bytes || !patches[i].data_ptr || patches[i].bytes > N1X_DLA_MAX_IMAGE - total) {
            err = -EINVAL;
            goto free_copies;
        }
        total += patches[i].bytes;
        copies[i] = memdup_user(u64_to_user_ptr(patches[i].data_ptr), patches[i].bytes);
        if (IS_ERR(copies[i])) {
            err = PTR_ERR(copies[i]);
            copies[i] = NULL;
            goto free_copies;
        }
    }
    mutex_lock(&accel->lock);
    if (accel->unplugged || accel->stopped) {
        err = -ENODEV;
        goto unlock;
    }
    list_for_each_entry(candidate, &accel->jobs, link)
        if (candidate->id == req->job_id && candidate->owner == file) {
            job = candidate;
            break;
        }
    if (!job)
        goto unlock;
    if (job->task.error || !job->task.verified || job->task.resumable || job->task.suspended) {
        err = -EIO;
        goto unlock;
    }
    if (accel->last_id == U64_MAX) {
        err = -EOVERFLOW;
        goto unlock;
    }
    for (i = 0; i < req->num_patches; i++) {
        bool writable = false;
        if (patches[i].offset >= job->image_bytes || patches[i].bytes > job->image_bytes - patches[i].offset) {
            err = -EINVAL;
            goto unlock;
        }
        for (j = 0; j < job->num_writable; j++) {
            u64 delta;
            if (job->writable[j].offset > patches[i].offset)
                continue;
            delta = patches[i].offset - job->writable[j].offset;
            if (delta < job->writable[j].bytes && patches[i].bytes <= job->writable[j].bytes - delta) {
                writable = true;
                break;
            }
        }
        if (!writable) {
            err = -EPERM;
            goto unlock;
        }
        for (j = 0; j < i; j++)
            if (patches[i].offset < patches[j].offset + patches[j].bytes &&
                patches[j].offset < patches[i].offset + patches[i].bytes) {
                err = -EINVAL;
                goto unlock;
            }
    }
    /* Only the owner explicitly opts into this successful-task I/O reuse.
     * Program, address list and allocation remain resident. This is neither
     * error recovery nor a whole-device DMA-free/unmap boundary.
     */
    for (i = 0; i < req->num_patches; i++)
        memcpy(job->task.buffer + N1X_JOB_PREFIX + patches[i].offset, copies[i], patches[i].bytes);
    job->task = (struct n1x_dla_job){.buffer = job->task.buffer, .dma = job->task.dma};
    job->id = ++accel->last_id; /* Old READ IDs cannot observe the replacement. */
    ops = (struct n1x_dla_job_ops){job, publish, read_fence};
    err = accel->submit(drm->dev, &job->task, &ops);
    if (!job->task.attempted && !err)
        err = -EIO;
    req->new_job_id = job->id;
    req->status = job->task.error = err;
    req->completed = !err && job->task.verified;
    err = 0; /* Execution status is separate from syscall/copy failure. */
unlock:
    mutex_unlock(&accel->lock);
free_copies:
    for (i = 0; i < req->num_patches; i++)
        kfree(copies[i]);
    kfree(copies);
    kfree(patches);
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
int n1x_dla_accel_stop(struct n1x_dla_accel *accel, int (*stop)(struct device *))
{
    struct n1x_accel_job *job, *next;
    int err;

    if (!accel || !stop)
        return -EINVAL;
    mutex_lock(&accel->lock);
    /* Wait for any synchronous EXEC to return before entering the backend.
     * This stays set on failure: administrative stop cannot revive DMA.
     */
    accel->stopped = true;
    err = stop(accel->drm.dev);
    if (!err) {
        list_for_each_entry_safe(job, next, &accel->jobs, link) {
            list_del(&job->link);
            dma_free_coherent(accel->drm.dev, job->allocated,
                              job->task.buffer, job->task.dma);
            kfree(job->writable);
            kfree(job);
        }
        accel->retained = 0;
        accel->count = 0;
        /* IDs are never recycled while this DRM device can have open FDs. */
    }
    mutex_unlock(&accel->lock);
    return err;
}
/* No GEM mmap: all device-visible bytes are immutable after EXEC's snapshot. */
static int n1x_accel_open(struct inode *inode, struct file *file)
{
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    return accel_open(inode, file);
}
static const struct file_operations accel_fops = {
    .fop_flags = FOP_UNSIGNED_OFFSET,
    .owner = THIS_MODULE, .open = n1x_accel_open, .release = drm_release,
    .unlocked_ioctl = drm_ioctl, .compat_ioctl = drm_compat_ioctl,
    .llseek = noop_llseek,
};
static const struct drm_ioctl_desc accel_ioctls[] = {
    DRM_IOCTL_DEF_DRV(N1X_DLA_INFO, info_ioctl, 0),
    DRM_IOCTL_DEF_DRV(N1X_DLA_EXEC, exec_ioctl, 0),
    DRM_IOCTL_DEF_DRV(N1X_DLA_READ, read_ioctl, 0),
    DRM_IOCTL_DEF_DRV(N1X_DLA_REEXEC, reexec_ioctl, 0),
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
