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
    __u32 feature_mask, general_word;
    __u16 general_header[3], allocation_words[4];
    __u8 boot_tables_valid;
};

/* Decode three already located BIT 'l' subtables (directory indices 2, 3, 4)
 * into host-endian fields. Bounds and the observed first-byte version gate
 * (>= 0x10) are checked. No ROM access, calibration, override selection or
 * hardware programming. Raw header/flag/tuning values have no inferred units.
 * The caller must prove ROM provenance and locate each buffer within that ROM;
 * this is not a complete VBIOS directory parser or power-policy validator.
 * General/allocation tables are not inputs to this partial decoder, so it
 * leaves boot_tables_valid=0. Use the full ROM parser for startup inputs.
 * Output is unchanged on failure; success does not authorize applying policy.
 */
int n1x_dla_decode_vbios_policy(const __u8 *frequency, size_t frequency_size,
                                 const __u8 *tuning, size_t tuning_size,
                                 const __u8 *gating, size_t gating_size,
                                 struct n1x_dla_vbios_policy *out);

struct n1x_dla_vbios_location {
    __u32 bit_offset, rom_base, directory_offset;
    __u32 frequency_offset, tuning_offset, gating_offset;
};

/* Find one unambiguous BIT 'l' policy in an immutable ROM window, at most
 * 0x110000 bytes. Checks checksum, all nested bounds, nonzero table pointers
 * and version gates for directory indices 0..4, including the general feature
 * mask and raw allocation words. Success sets boot_tables_valid=1. Uses the
 * Windows preceding aligned AA55 ROM-base rule; at most 32 checksummed BIT
 * headers are examined. No physical
 * mapping, file access or hardware writes. Both outputs unchanged on failure.
 * This locates data, not firmware provenance or authority to program hardware.
 */
int n1x_dla_parse_vbios_policy(const __u8 *data, size_t size,
                                struct n1x_dla_vbios_policy *out,
                                struct n1x_dla_vbios_location *location);

struct n1x_dla_gating_policy {
    __u32 request_mask;
    /* Windows outer bytes 0x31d..0x325, retained without invented enum names. */
    __u8 policy_bytes[9];
    __u8 slcg_override;
    /* Windows HAL bytes 0x47d, 0x47e, 0x484, 0x485. */
    __u8 hal_flags[4];
};

/* Derive normal HAL-1 defaults from a fully decoded board policy. Implements
 * the observed low-to-high feature-bit order, PPS zero substitutions and
 * request-mask calculation. No registry overrides or pre-silicon owner mode.
 * Rejects a partial decoder result and feature bit 0 (global init rejection).
 * Unknown feature bits have no effect, as in the pinned Windows updater.
 * Output unchanged on failure. No hardware access or physical power claim;
 * caller still establishes provenance and implements all required setup.
 */
int n1x_dla_prepare_gating(const struct n1x_dla_vbios_policy *policy,
                            struct n1x_dla_gating_policy *out);
#endif
