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
/* Serialize the verified 16-byte region record: printf (1), trace (3), or HAL-1 ring (5).
 * Address/size validity and DMA ownership are caller responsibilities. The
 * coherent command record must itself be 256-byte aligned and addressable by
 * method data = record IOVA >> 8 (40-bit range). The trace buffer requires a
 * valid initialized ring header. HAL-1 region 5 reads eight header bytes and
 * normalizes a zero/zero pair to 8/8 in firmware-local state. It requires at
 * least eight bytes here; caller owns valid backing, indices and DMA lifetime.
 * Region 5 is not supported by the inspected HAL-2 image. Serialization alone
 * does not configure a region or establish successful ring operation.
 * Zero/unaligned addresses, empty/wrapping regions and unknown IDs are rejected
 * as a Linux API policy. Output remains unchanged on error.
 */
int n1x_dla_build_region(__u8 *out, size_t out_size, __u32 region,
                         __u64 address, __u32 bytes);
/* Serialize the raw HAL-1 command-23 payload built by Windows RVA d45390.
 * Byte 0 is 1; byte 1 normalizes context byte 0x47d. If context u32 0x490
 * is nonzero, LE u32 at offset 2 is provider field +0xf4 multiplied by 2000,
 * modulo 2^32 exactly as Windows. Other bytes are zero. Field units and the
 * live P16 provider value are not inferred. This does not allocate/sync DMA,
 * send a method, or authorize guessed operating values. Caller supplies the
 * validated provider input and owns aligned DMA backing and its lifetime.
 * At least 40 output bytes are required; invalid output remains unchanged.
 */
int n1x_dla_build_command23(__u8 *out, size_t out_size, __u8 context_47d,
                            __u32 context_490, __u32 provider_f4);

#define N1X_DLA_CONFIG_BYTES 252U
/* Modify a previously exported HAL-1 config in place, matching Windows d47830
 * with no effective override-table entries and defaults +47e=1/+480=1.
 * Changes only bytes 1->2 and 0x32->1. Do not infer field units or translate
 * arbitrary user settings from this helper. Requires the region-5 allocation
 * when used by the default init sequence; the caller owns DMA visibility,
 * addressability, command 21 submission, response validation and lifetime.
 * Invalid arguments leave all bytes unchanged; trailing allocation bytes are
 * untouched. This does not submit or establish full firmware initialization.
 */
int n1x_dla_prepare_default_config(__u8 *buffer, size_t buffer_size);

#endif
