/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_RESPONSE_H
#define N1X_DLA_RESPONSE_H
#include "n1x_dla_io.h"
#define N1X_DLA_RESPONSE_ACK 0xd1a0cafeU
struct n1x_dla_response {
    __u32 word;
    __u32 data;
};
/* Capture/clear/ack one SWGEN1 response. Caller serializes IRQ handling and
 * supplies a powered, mapped device. Returns -EAGAIN for no pending SWGEN1.
 * Output is unchanged on error. After a clear/ack error, recovery/quiescence
 * is required; do not free DMA buffers or assume a retry is harmless.
 * Command-complete does not imply an inference task has finished.
 */
int n1x_dla_take_response(const struct n1x_dla_io *io,
                          struct n1x_dla_response *response);
#endif
