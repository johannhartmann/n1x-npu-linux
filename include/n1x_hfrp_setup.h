/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_HFRP_SETUP_H
#define N1X_HFRP_SETUP_H
#include "n1x_hfrp.h"
#include "n1x_dla_vbios.h"

struct n1x_hfrp_setup_result {
	struct n1x_hfrp_exchange_result exchange;
	__u32 frequency_khz[8];
	__u16 voltage_word[8];
	__u16 command;
	__u8 completed_commands;
	__u8 curve_count;
	__u8 configured;
};

/* Send the HAL-1 VBIOS policy sequence (SRAM_HOLD, CONFIG, GET_VF_CURVE,
 * tuning, power cap, and v1.5+ statistics setup) over an initialized HFRP channel.
 */
int n1x_hfrp_configure_policy(const struct n1x_dla_io *io,
			      struct n1x_hfrp_channel *channel,
			      const struct n1x_hfrp_version *version,
			      const struct n1x_dla_vbios_policy *policy,
			      __u32 polls, __u32 interval_us,
			      struct n1x_hfrp_setup_result *result);
#endif
