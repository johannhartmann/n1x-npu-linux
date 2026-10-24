/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_SROOT_H
#define N1X_SROOT_H
#include "n1x_dla_io.h"
#define N1X_SROOT_MAX_WORDS 14
#define N1X_SROOT_CMD_UPLOAD 0x18
#define N1X_SROOT_CMD_REINIT 0x1a
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
/* Prepare an exclusively owned idle mailbox, following Windows RVA 0x54c0.
 * This intentionally discards stale reply/echo notifications. Never call it
 * with an outstanding request or as proof that DMA has quiesced. All stages
 * are bounded; timeout/error requires recovery, not automatic resubmission.
 * A nonzero low byte at 11*stride is reported as -EBUSY.
 */
int n1x_sroot_prepare(const struct n1x_dla_io *io, __u32 stride,
                      __u32 polls, __u32 interval_us);
/* Stage an opaque method-2 image and build command 0x18's three words.
 * Caller supplies CPU-accessible DMA storage and its logical device address;
 * words must not overlap image/storage. Image/storage may overlap (memmove).
 * Requires at least the Windows DMF minimum of 32 bytes and a fully 32-bit
 * addressable allocation within the supplied inclusive DMA window, with the Windows
 * end-minus-start input-length limit. For the
 * examined P16 INF, that window is 0xe0000000..0xffffffff. Copies image[4:] and zeros the otherwise untouched
 * final four bytes. No signature/authentication validation is implemented.
 * Caller must establish mapping/domain, bounds and coherency before submitting.
 * Returns without modifying outputs on invalid input. Does not submit/free DMA.
 */
int n1x_sroot_stage_image(__u8 *buffer, size_t capacity,
                          const __u8 *image, size_t size,
                          __u64 dma_address, __u32 dma_minimum,
                          __u32 dma_maximum, __u32 words[3]);
/* One serialized prepare -> TX/echo -> RX transaction. Caller owns an idle
 * mailbox for the entire call, any DMA storage, and any global deadline.
 * Each polling stage uses the supplied bounded budget. No retries, DMA frees
 * or reset/recovery are performed. Reply is published only on transport
 * success, which DOES NOT imply firmware status/detail success or matching
 * request identity. Validate these before treating an operation as completed.
 * After an attempted write, an error leaves delivery/ownership uncertain.
 */
int n1x_sroot_exchange(const struct n1x_dla_io *io, __u32 stride,
                       __u8 command, const __u32 *words, __u8 count,
                       struct n1x_sroot_reply *reply,
                       __u32 polls, __u32 interval_us);
/* Build the separate reinitialization command's payload. Windows input is
 * little-endian tag (4 bytes, implicit mode 1), or tag + mode (8 bytes).
 * Accepted modes are 1/2; output is [tag, 0, mode], sent with command 0x1a.
 * P16 KMD subtype 1 uses input tag 0xc0302, mode 2. No DMA storage is referenced
 * by these three words. This only serializes: do not equate reinit reply with
 * proven NPU boot or DMA quiescence. Outputs unchanged on invalid input.
 */
int n1x_sroot_build_reinit(__u32 words[3], const __u8 *request, size_t size);
#endif
