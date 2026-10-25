/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_BOOT_H
#define N1X_DLA_BOOT_H
#include "n1x_sroot.h"

struct n1x_dla_sroot_load_result {
    struct n1x_sroot_reply reply;
    /* Conservative from BEFORE any mailbox operation, even on prepare errors.
     * The caller must retain upload DMA storage until verified quiescence.
     * Neither a clear flag on invalid input nor a later reinit says anything
     * about storage published by an earlier transaction.
     */
    __u8 upload_attempted;
    __u8 reply_valid;
    __u8 idle_observed;
};
/* Initial method-2 upload phase only: SROOT exchange, firmware status check,
 * then the Windows method-2 write-free IDLESTATE wait. Does NOT select/start
 * the core, capture routes, mask/enable IRQs, ping or initialize regions/config.
 * A zero return is not firmware readiness, task completion or DMA quiescence.
 *
 * Caller exclusively owns both powered/mapped endpoints for the whole call.
 * words must come from n1x_sroot_stage_image on validated firmware and a DMA
 * allocation belonging to SROOT, with established domain/visibility. Caller
 * serializes, supplies barriers and retains that allocation on every attempted
 * upload, including success/timeouts/firmware rejection. No freeing or retry.
 * All pointers/objects must be distinct and remain valid through the call.
 *
 * Each stage has a separate poll budget (not the Windows global deadline).
 * Invalid callback/poll input leaves result unchanged and performs no I/O.
 * Otherwise result is published progressively, including on failure. A raw
 * reply is preserved even when its status/detail fail. Windows does not check
 * reply command identity here; preserve it for qualification, do not invent
 * an echo requirement. The live reply format still needs hardware validation.
 */
int n1x_dla_sroot_upload(const struct n1x_dla_io *sroot,
                         const struct n1x_dla_io *dla, __u32 stride,
                         const __u32 words[3], __u32 polls, __u32 interval_us,
                         struct n1x_dla_sroot_load_result *result);
#endif
