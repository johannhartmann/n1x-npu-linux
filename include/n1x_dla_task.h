/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_TASK_H
#define N1X_DLA_TASK_H
#include "n1x_dla_firmware.h"
#define N1X_DLA_TASK_V2_BYTES 60
#define N1X_DLA_FENCED_TASK_V2_BYTES 96
struct n1x_dla_task_args {
    __u64 next;
    __u64 address_list;
    __u64 timeout;
    __u16 num_addresses;
};
/* Build a queue-0 v2 descriptor with no actions, flags or extensions. Pure
 * serialization; the common header is accepted by both HAL-1 RV32 and T35x
 * RV64 checks. This does not establish DMA ownership, address reachability or
 * full firmware compatibility. Caller must provide a 256-byte-aligned address-list IOVA, mapped valid buffers and
 * keep them alive until completion/quiescence. Output is unchanged on failure.
 */
int n1x_dla_build_task_v2(__u8 *out, size_t out_size,
                         const struct n1x_dla_task_args *args);
/* Adds a terminal WRITE_SEM postaction, including DMA lookahead padding.
 * Caller must initialize a coherent fence word to a different value before
 * submission and inspect task error/status as well as the fence. The value
 * does not prove success, nor provide a CPU memory barrier or DMA ownership.
 * Same lifetime and unchanged-on-failure rules as the basic serializer.
 */
int n1x_dla_build_fenced_task_v2(__u8 *out, size_t out_size,
                                const struct n1x_dla_task_args *args,
                                __u64 fence_address, __u32 fence_value);
/* Cached-path address list (1..512 entries), padded for firmware DMA reads.
 * Source and output must not overlap. DMA mapping/alignment, model index bounds
 * and buffer lifetimes remain caller responsibilities. Addresses are serialized
 * verbatim, including zero; this does not validate model memory references.
 * The firmware permits index == count, so host validation must require < count.
 * Size helper returns zero for unsupported counts. Output unchanged on error.
 */
size_t n1x_dla_address_list_bytes(__u16 count);
int n1x_dla_build_address_list(__u8 *out, size_t out_size,
                              const __u64 *addresses, __u16 count);
struct n1x_dla_buffer_range {
    __u64 address;
    __u64 bytes;
};
struct n1x_dla_il_range {
    __u64 address;
    __u32 dma_bytes;
    __u16 instructions;
};
/* Validate one recovered 12-byte IL slice (index/offset/count), including all
 * firmware cache reads. Does not validate instruction opcodes, their embedded
 * memory/register references, or the complete model. Buffers must describe
 * real mapped allocations, including accessible padding. Output unchanged on
 * error. Reserved slice bytes are not interpreted.
 */
int n1x_dla_validate_il_slice(struct n1x_dla_il_range *out,
                              const __u8 *slice, size_t slice_size,
                              const struct n1x_dla_buffer_range *buffers,
                              __u16 num_buffers);
#endif
