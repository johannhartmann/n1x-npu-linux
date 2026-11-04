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

/* Capture and acknowledge the HFRP aperture interrupt output (0x104 / 0x10c). */
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

/* Enqueue one HFRP command or dequeue one response on the 116-byte circular rings. */
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

int n1x_hfrp_discover_version(const struct n1x_dla_io *io,
			      struct n1x_hfrp_channel *channel, __u32 polls,
			      __u32 interval_us, struct n1x_hfrp_version *version,
			      struct n1x_hfrp_exchange_result *result);

/* Request clock, power domain, and rail ON (Windows request mask 0x07). */
int n1x_hfrp_power_on(const struct n1x_dla_io *io,
		      struct n1x_hfrp_channel *channel,
		      const struct n1x_hfrp_version *version, __u8 pps,
		      __u32 polls, __u32 interval_us,
		      struct n1x_hfrp_exchange_result *result);

struct n1x_hfrp_power_state {
	__u8 raw;
	__u8 valid;
};

/* Query HFRP power/gating state via command 0x1af (protocol v1.5+). */
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

/* Request immediate clock/domain/rail OFF (wire 0x0043) and verify state 0x1af low bits == 0x07. */
int n1x_hfrp_stop(const struct n1x_dla_io *io,
		  struct n1x_hfrp_channel *channel,
		  const struct n1x_hfrp_version *version,
		  __u32 polls, __u32 interval_us,
		  struct n1x_hfrp_stop_result *result);
#endif
