/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_RESPONSE_H
#define N1X_DLA_RESPONSE_H
#include "n1x_dla_io.h"

#define N1X_DLA_RESPONSE_ACK 0xd1a0cafeU

struct n1x_dla_response {
	__u32 word;
	__u32 data;
};

/* Capture, clear, and acknowledge one pending SWGEN1 mailbox response. */
int n1x_dla_take_response(const struct n1x_dla_io *io,
			  struct n1x_dla_response *response);

struct n1x_dla_response_fields {
	__u8 message;
	__u8 command;
	__u8 status;
	__u8 upper;
	__u32 data;
};

int n1x_dla_decode_response(const struct n1x_dla_response *response,
			    struct n1x_dla_response_fields *fields);
#endif
