/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_VBIOS_H
#define N1X_DLA_VBIOS_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

struct n1x_dla_vbios_policy {
    __u32 frequency_khz, entry_frequency_khz, exit_frequency_khz;
    __u32 power_cap_word;
    __u32 tuning[9];
    __u32 gating_word;
    __u16 frequency_header[2], tuning_header[2], gating_header[2];
    __u16 gating_tail[2];
    __u8 frequency_flags[2];
    __u8 gating_flags;
    __u8 gating_bytes[3];
};

/* Decode three already located BIT 'l' subtables (directory indices 2, 3, 4)
 * into host-endian fields. Bounds and the observed first-byte version gate
 * (>= 0x10) are checked. No ROM access, calibration, override selection or
 * hardware programming. Raw header/flag/tuning values have no inferred units.
 * The caller must prove ROM provenance and locate each buffer within that ROM;
 * this is not a complete VBIOS directory parser or power-policy validator.
 * Output is unchanged on failure; success does not authorize applying policy.
 */
int n1x_dla_decode_vbios_policy(const __u8 *frequency, size_t frequency_size,
                                 const __u8 *tuning, size_t tuning_size,
                                 const __u8 *gating, size_t gating_size,
                                 struct n1x_dla_vbios_policy *out);
#endif
