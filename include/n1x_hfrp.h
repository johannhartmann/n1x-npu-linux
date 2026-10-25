/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_HFRP_H
#define N1X_HFRP_H
#include "n1x_dla_io.h"

struct n1x_hfrp_irq_result {
    __u32 status;
    __u8 status_valid;
    __u8 ack_attempted;
    __u8 acknowledged;
};
/* Capture/ack the separate HFRP aperture's interrupt output (0x104 / 0x10c).
 * io offsets are HFRP-relative, NOT DLA logical/remapped or SROOT registers.
 * Caller establishes the correct powered resource and exclusively serializes
 * interrupt handling. No power/clock/reset/doorbell-response processing occurs.
 * Preserve/queue the returned raw status, including unknown bits; do not infer
 * that acknowledgment completed the event's required higher-level work.
 *
 * Invalid arguments leave output unchanged/no I/O. Otherwise fields progress:
 * successful read sets status_valid; status zero returns -EAGAIN without a
 * no-op clear; nonzero status is written verbatim to IRQ_OUT_CLR as Windows.
 * An ACK error preserves the captured status with ack_attempted=1, acknowledged=0.
 * No retry, IRQ registration, buffer release or quiescence claim is made.
 */
int n1x_hfrp_take_irq(const struct n1x_dla_io *io,
                     struct n1x_hfrp_irq_result *result);
#endif
