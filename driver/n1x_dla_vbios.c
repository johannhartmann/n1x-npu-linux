/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_vbios.h"

static __u16 get16(const __u8 *p)
{
    return (__u16)p[0] | (__u16)p[1] << 8;
}

static __u32 get32(const __u8 *p)
{
    return (__u32)get16(p) | (__u32)get16(p + 2) << 16;
}

int n1x_dla_decode_vbios_policy(const __u8 *frequency, size_t frequency_size,
                                 const __u8 *tuning, size_t tuning_size,
                                 const __u8 *gating, size_t gating_size,
                                 struct n1x_dla_vbios_policy *out)
{
    struct n1x_dla_vbios_policy parsed = {0};
    unsigned int i;

    if (!frequency || !tuning || !gating || !out)
        return -EINVAL;
    if (frequency_size < 18 || tuning_size < 32 || gating_size < 16)
        return -EMSGSIZE;
    if (frequency[0] < 0x10 || tuning[0] < 0x10 || gating[0] < 0x10)
        return -EOPNOTSUPP;
    for (i = 0; i < 2; i++) {
        parsed.frequency_header[i] = get16(frequency + i * 2);
        parsed.tuning_header[i] = get16(tuning + i * 2);
        parsed.gating_header[i] = get16(gating + i * 2);
        parsed.gating_tail[i] = get16(gating + 12 + i * 2);
        parsed.frequency_flags[i] = frequency[4 + i];
    }
    parsed.frequency_khz = get32(frequency + 6);
    parsed.entry_frequency_khz = get32(frequency + 10);
    parsed.exit_frequency_khz = get32(frequency + 14);
    parsed.power_cap_word = get32(tuning + 4);
    parsed.tuning[0] = get32(tuning + 8);
    for (i = 0; i < 3; i++) {
        parsed.tuning[1 + i] = get16(tuning + 12 + i * 2);
        parsed.tuning[6 + i] = get16(tuning + 26 + i * 2);
        parsed.gating_bytes[i] = gating[9 + i];
    }
    parsed.tuning[4] = get32(tuning + 18);
    parsed.tuning[5] = get32(tuning + 22);
    parsed.gating_flags = gating[4];
    parsed.gating_word = get32(gating + 5);
    *out = parsed;
    return 0;
}
