/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_COMMAND_H
#define N1X_DLA_COMMAND_H
#include "n1x_dla_io.h"
/* Submit a method after the previous method has left the interface. Caller
 * serializes commands, prepares DMA visibility before this call and retains
 * all referenced buffers until independently verified completion/quiescence.
 * This does not wait for firmware completion. An error on the final ID write
 * does not establish that firmware did not receive the command; do not retry
 * or release DMA storage based only on this return value.
 * Poll policy is the same bounded Linux policy as n1x_dla_start_core.
 */
int n1x_dla_send_method(const struct n1x_dla_io *io, __u32 method,
                       __u32 data, __u32 polls, __u32 interval_us);
/* Serialize the verified 16-byte printf (1) or trace (3) region record.
 * Address/size validity and DMA ownership are caller responsibilities. The
 * coherent command record must itself be 256-byte aligned and addressable by
 * method data = record IOVA >> 8 (40-bit range). The trace buffer requires a
 * valid initialized ring header; serialization alone does not configure it.
 * Zero/unaligned addresses, empty/wrapping regions and unknown IDs are rejected
 * as a Linux API policy. Output remains unchanged on error.
 */
int n1x_dla_build_region(__u8 *out, size_t out_size, __u32 region,
                         __u64 address, __u32 bytes);
#endif
