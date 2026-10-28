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
    __u8 space_unavailable;
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

enum n1x_hfrp_channel_state {
    N1X_HFRP_NEW = 0,
    N1X_HFRP_READY,
    N1X_HFRP_ACTIVE,
    N1X_HFRP_FAILED,
};

#define N1X_HFRP_NO_SEQUENCE 0xffffU

/* Zero initialize once for a separately established, exclusively owned device
 * lifetime. A failed channel cannot be re-handshaken by these APIs. Resetting
 * this struct is NOT recovery: hardware quiescence must be established first.
 * Caller holds its lock throughout each API and all callbacks. async_reply may
 * copy/deliver a consumed asynchronous response and returns zero or negative
 * errno; it must not reenter or retain the pointer. A missing/failing callback
 * fails the channel instead of silently
 * losing a notification. No IRQ handler may consume these rings concurrently.
 */
struct n1x_hfrp_channel {
    enum n1x_hfrp_channel_state state;
    __u16 next_sequence;
    __u16 pending_sequence;
    void *async_context;
    int (*async_reply)(void *context, const struct n1x_hfrp_reply *reply);
};

struct n1x_hfrp_exchange_result {
    struct n1x_hfrp_tx_result tx;
    struct n1x_hfrp_reply reply;
    __u32 async_count;
    __u32 discarded_startup_replies;
    __u8 reply_valid;
    __u8 reset_attempted;
    __u8 reset_completed;
    __u8 peer_ready;
    __u8 ack_attempted;
    __u8 ack_completed;
    __u8 mode_select_attempted;
    __u8 mode_selected;
};

/* Both APIs are synchronous, bounded polling operations, not kernel bindings.
 * polls is 1..5001; interval_us is passed to delay_us between unsuccessful
 * polls (required when polls > 1 and interval_us != 0). Invalid parameters and
 * state-gate failures leave channel/result unchanged and perform no I/O.
 * Handshake on NEW selects circular mode (register 4 = 1), then resets the
 * HFRP transport. It does not power on or
 * authenticate the NPU. Startup replies are drained/counted after reset, before
 * command 1 is issued. The matched reply is retained; no undocumented payload
 * interpretation is imposed. Reset timeout and firmware errors fail handshake.
 *
 * Exchange requires READY. One pending sequence is retained until matching
 * reply consumption and doorbell acknowledgment finish. A completed firmware
 * error returns -EREMOTEIO with reply_valid=1 and leaves the channel usable.
 * Timeout, access/protocol/notification errors poison the channel, even when
 * a reply has already been consumed. No cancellation, retry or automatic reset
 * occurs. A full command ring before publication (-ENOSPC) remains retryable.
 * READY means transport completion only, not success of a power operation.
 * Other IRQ bits are left untouched for the owning device's event handling.
 */
int n1x_hfrp_handshake(const struct n1x_dla_io *io,
                        struct n1x_hfrp_channel *channel, __u32 polls,
                        __u32 interval_us, struct n1x_hfrp_exchange_result *result);
int n1x_hfrp_exchange(const struct n1x_dla_io *io,
                       struct n1x_hfrp_channel *channel, __u16 command,
                       const __u8 *payload, __u32 length, __u32 polls,
                       __u32 interval_us, struct n1x_hfrp_exchange_result *result);

struct n1x_hfrp_version {
    __u32 stamp;
    __u32 response_word;
    __u8 stamp_valid;
    __u8 query_requested;
    __u8 valid;
    __u8 major;
    __u8 minor;
};

/* Requires a READY channel. Reads offset 0, follows the recovered fixed-version
 * thresholds or issues command 999. Query replies must have status zero and
 * exactly four payload bytes. An accepted discovery attempt clears the old
 * version and marks it valid only on success; argument/state rejection leaves
 * outputs unchanged.
 * All decoded versions can be reported; power-on accepts only major 1 with
 * minor 3 or >= 5, matching the recovered Windows format selection.
 * Access errors/truncated replies poison the channel; an unsupported old stamp
 * or confirmed firmware error does not imply successful discovery.
 */
int n1x_hfrp_discover_version(const struct n1x_dla_io *io,
                               struct n1x_hfrp_channel *channel, __u32 polls,
                               __u32 interval_us, struct n1x_hfrp_version *version,
                               struct n1x_hfrp_exchange_result *result);

/* One POWER_CONTROL command corresponding to Windows request mask 7:
 * clock, power domain and rail ON. No implicit SRAM/config/tuning/limit writes.
 * Caller must establish required startup configuration first and check success
 * before loading firmware. v1.3 carries an explicit caller-supplied raw PPS byte;
 * v1.5+ omits it. This API does not choose/calibrate PPS or prove power is on.
 * An accepted response is command completion, not a measured physical state.
 */
int n1x_hfrp_power_on(const struct n1x_dla_io *io,
                       struct n1x_hfrp_channel *channel,
                       const struct n1x_hfrp_version *version, __u8 pps,
                       __u32 polls, __u32 interval_us,
                       struct n1x_hfrp_exchange_result *result);

struct n1x_hfrp_power_state {
    __u8 raw;
    __u8 valid;
};
/* Query command 0x1af, with no request payload, on protocol major 1/minor >=5.
 * The original Windows wakeup policy consumes response bits 0..2. Preserve
 * the whole byte without interpreting it as engine idle or DMA quiescence.
 * Caller owns an established READY channel and serializes this with power,
 * reset and device operations. This is a firmware-reported snapshot, not an
 * independent measurement or permission to reclaim buffers.
 *
 * Argument/version/state rejection preserves all outputs and performs no I/O.
 * An accepted attempt clears old state; valid is set only after a successful
 * exchange with exactly one payload byte. Firmware errors propagate with the
 * exchange diagnostics; malformed lengths fail the channel with -EBADMSG.
 * No implicit handshake, power change, reset, retry or fallback read occurs.
 */
int n1x_hfrp_query_power_state(const struct n1x_dla_io *io,
                                struct n1x_hfrp_channel *channel,
                                const struct n1x_hfrp_version *version,
                                __u32 polls, __u32 interval_us,
                                struct n1x_hfrp_power_state *state,
                                struct n1x_hfrp_exchange_result *result);

struct n1x_hfrp_stop_result {
    struct n1x_hfrp_exchange_result power;
    struct n1x_hfrp_exchange_result query;
    struct n1x_hfrp_power_state state;
    __u8 attempted;
    __u8 accepted;
    __u8 confirmed;
};
/* Immediate clock/domain/rail OFF (Windows request 0x70 -> wire 0x0043).
 * Requires v1.5+, a READY exclusively owned channel and caller-drained work.
 * Checks the complete two-byte power reply, then requires the low three gating bits of query 0x1af to equal 7.
 * These bits report gating, NOT task completion. Caller must separately prove
 * successful retirement of every DMA publisher, including the SROOT upload.
 * No reset, retry, delayed gating, forced unmap or implicit memory release.
 * Any attempted failure leaves the caller's device/lifetime quarantined.
 * Invalid input/state/version preserves result and performs no I/O.
 */
int n1x_hfrp_stop(const struct n1x_dla_io *io,
                   struct n1x_hfrp_channel *channel,
                   const struct n1x_hfrp_version *version,
                   __u32 polls, __u32 interval_us,
                   struct n1x_hfrp_stop_result *result);
#endif
