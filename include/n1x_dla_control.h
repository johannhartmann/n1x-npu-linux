/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_CONTROL_H
#define N1X_DLA_CONTROL_H
#include "n1x_dla_response.h"

struct n1x_dla_control_result {
	struct n1x_dla_response response;
	__u8 method_attempted;
	__u8 response_valid;
};

/* Execute a synchronous HAL-1 DMA control transaction (commands 11, 21, 22, 23, 24). */
int n1x_dla_control_command(const struct n1x_dla_io *io, __u8 command,
			    __u64 dma_address, __u32 polls, __u32 interval_us,
			    struct n1x_dla_control_result *result);
#endif
