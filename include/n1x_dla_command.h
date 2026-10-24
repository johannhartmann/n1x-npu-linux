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
#endif
