/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_CONTROL_H
#define N1X_DLA_CONTROL_H
#include "n1x_dla_response.h"

struct n1x_dla_control_result {
    struct n1x_dla_response response;
    __u8 method_attempted;
    __u8 response_valid;
};
/* Serialized, polled HAL-1 DMA control transaction for commands 11/21/22/23/24.
 * Task command 7 is deliberately excluded: its 0x702 reply is acceptance, not
 * task completion. Caller exclusively owns powered endpoints/mailbox handling,
 * supplies initialized/visible command-specific DMA backing, and retains it
 * through independent verified completion/quiescence. This helper never frees.
 * It validates only the 256-byte-aligned, nonzero, 40-bit encoded address, not
 * the allocation extent, coherency, payload schema or target's DMA mask.
 *
 * A preexisting SWGEN1 response causes -EBUSY without consuming it or sending.
 * Otherwise send (0x300 | command), wait boundedly, capture/clear/ACK, then
 * require matching command/data, upper=0, status=0 and message=2. A captured
 * response is preserved even when rejected (-EPROTO/-EREMOTEIO). It is already
 * ACKed: unexpected responses require recovery, not blind resubmission.
 *
 * Invalid input leaves output unchanged/no I/O. Thereafter result is progressive;
 * method_attempted is conservative even on a pre-send pending timeout. Errors
 * during ACK may leave response_valid false despite firmware having executed.
 * Zero return proves the software response contract only, not full boot, all
 * IRQ cleanup, whole-device DMA quiescence or numerical inference. Each send
 * and response stage has a separate bounded Linux poll budget.
 */
int n1x_dla_control_command(const struct n1x_dla_io *io, __u8 command,
                            __u64 dma_address, __u32 polls, __u32 interval_us,
                            struct n1x_dla_control_result *result);
#endif
