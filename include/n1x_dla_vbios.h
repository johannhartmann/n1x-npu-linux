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

struct n1x_dla_vbios_location {
    __u32 bit_offset, rom_base, directory_offset;
    __u32 frequency_offset, tuning_offset, gating_offset;
};

/* Find one unambiguous BIT 'l' policy in an immutable ROM window, at most
 * 0x110000 bytes. Checks checksum, all nested bounds, nonzero table pointers
 * and the decoder's version gates. Uses the Windows preceding aligned AA55
 * ROM-base rule; at most 32 checksummed BIT headers are examined. No physical
 * mapping, file access or hardware writes. Both outputs unchanged on failure.
 * This locates data, not firmware provenance or authority to program hardware.
 */
int n1x_dla_parse_vbios_policy(const __u8 *data, size_t size,
                                struct n1x_dla_vbios_policy *out,
                                struct n1x_dla_vbios_location *location);
#endif
