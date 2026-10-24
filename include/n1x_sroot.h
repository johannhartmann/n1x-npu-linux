/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_SROOT_H
#define N1X_SROOT_H
#include "n1x_dla_io.h"
#define N1X_SROOT_MAX_WORDS 14
/* Transport-only TX for zero-status/zero-metadata request headers. io must use
 * the separate SROOT resource, not the DLA aperture. stride is the recovered
 * register spacing, still to be established on live hardware. Callbacks must
 * enforce resource bounds, serialize ordered MMIO and supply required barriers.
 * Caller serializes the entire request/response transaction, owns referenced
 * DMA storage and checks the later firmware reply. Success only verifies the
 * complemented TX echo. Errors after the first write leave delivery uncertain:
 * do not retry or release DMA memory without recovery/quiescence.
 * Polling policy is 1..5001 attempts, with a nonzero delay for multiple attempts.
 */
int n1x_sroot_send(const struct n1x_dla_io *io, __u32 stride,
                   __u8 command, const __u32 *words, __u8 count,
                   __u32 polls, __u32 interval_us);
/* Raw reply preserved alongside fields decoded by Windows RVA 0x4f70.
 * Transport success does not imply status == 0. Caller validates command,
 * status, detail and payload against its outstanding request. Output is only
 * published after all reads and acknowledgment succeed. Oversized replies and
 * errors leave recovery/ownership with the transaction owner; no auto retry.
 */
struct n1x_sroot_reply {
    __u32 header;
    __u32 words[N1X_SROOT_MAX_WORDS];
    __u16 status;
    __u8 command, detail, count;
};
int n1x_sroot_receive(const struct n1x_dla_io *io, __u32 stride,
                      struct n1x_sroot_reply *reply,
                      __u32 polls, __u32 interval_us);
#endif
