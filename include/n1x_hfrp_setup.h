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

/* Normal HAL-1 VBIOS-enabled startup packets, after handshake/discovery.
 * Caller establishes matching board policy, ownership and power, holds the
 * channel lock and calls once per device lifetime. No registry overrides.
 * Sends SRAM_HOLD, CONFIG (default delays 1/4/10 ms), GET_VF_CURVE, tuning,
 * cap query, and three statistics requests for 1.5+. No power-on or upload.
 *
 * Requires inferred reply lengths: SRAM 1, CONFIG 14/26, tuning 36,
 * cap query 4, statistics 26 bytes;
 * VF accepts compact 2+4*count or full 34-byte payload with 1..8 entries.
 * These are conservative host-side contracts inferred from Windows buffers
 * and L4T parsers, NOT verified replies from this board. Raw final/failed
 * reply is retained. Nonzero firmware status, malformed replies and transport
 * errors stop immediately and poison the channel: partial setup is not safe
 * to retry. Invalid inputs/state preserve output and perform no I/O.
 * configured means these exchanges completed under those contracts only;
 * it does not verify policy calibration, physical state or firmware boot.
 */
int n1x_hfrp_configure_policy(const struct n1x_dla_io *io,
                               struct n1x_hfrp_channel *channel,
                               const struct n1x_hfrp_version *version,
                               const struct n1x_dla_vbios_policy *policy,
                               __u32 polls, __u32 interval_us,
                               struct n1x_hfrp_setup_result *result);
#endif
