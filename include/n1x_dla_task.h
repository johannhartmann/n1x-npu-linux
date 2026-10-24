/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_TASK_H
#define N1X_DLA_TASK_H
#include "n1x_dla_firmware.h"
#define N1X_DLA_TASK_V2_BYTES 60
struct n1x_dla_task_args {
    __u64 next;
    __u64 address_list;
    __u64 timeout;
    __u16 num_addresses;
};
/* Build a queue-0 v2 descriptor with no actions, flags or extensions. Pure
 * serialization; it does not establish DMA ownership, address reachability or
 * full firmware compatibility. Caller must provide mapped, valid buffers and
 * keep them alive until completion/quiescence. Output is unchanged on failure.
 */
int n1x_dla_build_task_v2(__u8 *out, size_t out_size,
                         const struct n1x_dla_task_args *args);
#endif
