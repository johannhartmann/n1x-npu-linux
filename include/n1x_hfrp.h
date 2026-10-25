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

#define N1X_HFRP_RING_SIZE 116U
#define N1X_HFRP_REPLY_MAX 50U
#define N1X_HFRP_ASYNC_SEQ 0x3ffU

struct n1x_hfrp_tx_result {
    __u32 header;
    __u8 head_publish_attempted;
    __u8 head_published;
    __u8 doorbell_attempted;
    __u8 doorbell_written;
};

struct n1x_hfrp_reply {
    __u32 header;
    __u16 sequence;
    __u16 status;
    __u8 length;
    __u8 payload[N1X_HFRP_REPLY_MAX];
    __u8 tail_publish_attempted;
    __u8 tail_published;
};

/* Mode-0 Windows HFRP layout only: 116-byte command/response rings.
 * Caller establishes power, protocol/mode and sole ownership, serializes both
 * functions (including IRQ consumption), and uses ordered MMIO callbacks.
 * No DMA, handshake, reset, IRQ registration, sequence allocation or waiting.
 * Never reuse a sequence with an outstanding/uncertain request.
 *
 * Queue leaves one ring byte empty. It writes packet bytes, publishes the head,
 * then rings IRQ_IN_SET bit 1. After an attempted head publication, any error
 * may mean firmware owns the command: inspect result, do not blindly retry.
 *
 * Read consumes exactly one complete response, preserving nonzero firmware
 * status and async sequence 0x3ff for the caller. It does not match a pending
 * command, acknowledge interrupts or claim successful command execution.
 * -EAGAIN means empty/incomplete; -EPROTO invalid indices/short header;
 * -EMSGSIZE an oversized response. Those paths do not publish a tail.
 * Any tail-publication error leaves consumption uncertain: do not deliver or
 * retry the response without recovery. Other access errors propagate.
 * Invalid arguments leave result unchanged and perform no I/O.
 */
int n1x_hfrp_queue_command(const struct n1x_dla_io *io, __u16 command,
                           __u16 sequence, const __u8 *payload, __u32 length,
                           struct n1x_hfrp_tx_result *result);
int n1x_hfrp_read_reply(const struct n1x_dla_io *io,
                        struct n1x_hfrp_reply *reply);
#endif
