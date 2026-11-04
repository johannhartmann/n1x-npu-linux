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

/* Decode BIT 'l' (0x6c) subtables (directory indices 2, 3, 4) into host-endian fields. */
int n1x_dla_decode_vbios_policy(const __u8 *frequency, size_t frequency_size,
				const __u8 *tuning, size_t tuning_size,
				const __u8 *gating, size_t gating_size,
				struct n1x_dla_vbios_policy *out);

struct n1x_dla_vbios_location {
	__u32 bit_offset, rom_base, directory_offset;
	__u32 frequency_offset, tuning_offset, gating_offset;
};

/* Locate and parse the BIT 'l' (0x6c) policy table within a VBIOS image window. */
int n1x_dla_parse_vbios_policy(const __u8 *data, size_t size,
			       struct n1x_dla_vbios_policy *out,
			       struct n1x_dla_vbios_location *location);

/* Verify the PCI Expansion ROM Data Structure ("PCIR") matches N1X (0x10de:0x2e03). */
int n1x_dla_verify_vbios_pcir(const __u8 *data, size_t size,
			      const struct n1x_dla_vbios_location *location);

struct n1x_dla_gating_policy {
	__u32 request_mask;
	__u8 policy_bytes[9];
	__u8 slcg_override;
	__u8 hal_flags[4];
};

/* Derive HAL-1 power/clock gating defaults and request_mask from a parsed VBIOS policy. */
int n1x_dla_prepare_gating(const struct n1x_dla_vbios_policy *policy,
			   struct n1x_dla_gating_policy *out);
#endif
