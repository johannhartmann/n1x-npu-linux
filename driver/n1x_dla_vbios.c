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

static int fits(size_t offset, size_t length, size_t size)
{
    return offset <= size && length <= size - offset;
}

int n1x_dla_prepare_gating(const struct n1x_dla_vbios_policy *policy,
                            struct n1x_dla_gating_policy *out)
{
    struct n1x_dla_gating_policy result = {
        .request_mask = 7,
        .hal_flags = {1, 1, 0, 0},
    };
    unsigned int bit;

    if (!policy || !out || policy->boot_tables_valid != 1)
        return -EINVAL;
    if (policy->feature_mask & 1U)
        return -EOPNOTSUPP;
    result.policy_bytes[0] = !policy->frequency_flags[0];
    result.policy_bytes[1] = 1;
    result.policy_bytes[2] = policy->gating_bytes[1] ? policy->gating_bytes[1] : 1;
    result.policy_bytes[3] = !policy->gating_flags;
    result.policy_bytes[4] = 1;
    result.policy_bytes[5] = policy->gating_bytes[2] ? policy->gating_bytes[2] : 3;
    result.policy_bytes[6] = !policy->gating_flags;
    result.policy_bytes[7] = 1;
    result.policy_bytes[8] = 15;
    for (bit = 1; bit < 32; bit++) {
        if (!(policy->feature_mask & (1U << bit)))
            continue;
        switch (bit) {
        case 1: result.policy_bytes[0] = 1; break;
        case 2: result.slcg_override = 1; break;
        case 3:
            result.policy_bytes[3] = 1;
            result.policy_bytes[6] = 1;
            break;
        case 4: result.policy_bytes[6] = 1; break;
        case 5: case 6: case 7: case 8:
            result.hal_flags[bit - 5] = 0;
            break;
        case 17: result.policy_bytes[0] = 0; break;
        case 18: result.slcg_override = 0; break;
        case 19: result.policy_bytes[3] = 0; break;
        case 20:
            result.policy_bytes[6] = 0;
            result.policy_bytes[3] = 0;
            break;
        case 21: case 22: case 23: case 24:
            result.hal_flags[bit - 21] = 1;
            break;
        default: break;
        }
    }
    if (!result.policy_bytes[0])
        result.request_mask |= 0x110;
    if (!result.policy_bytes[3])
        result.request_mask |= 0x220;
    if (!result.policy_bytes[6])
        result.request_mask |= 0x440;
    *out = result;
    return 0;
}

int n1x_dla_parse_vbios_policy(const __u8 *data, size_t size,
                                struct n1x_dla_vbios_policy *out,
                                struct n1x_dla_vbios_location *location)
{
    struct n1x_dla_vbios_policy parsed = {0};
    struct n1x_dla_vbios_location found = {0};
    size_t i, base = 0;
    unsigned int have_base = 0, have_policy = 0, headers = 0;

    if (!data || !out || !location)
        return -EINVAL;
    if (size > 0x110000U)
        return -EFBIG;
    if (size < 12)
        return -EMSGSIZE;
    for (i = 0; i <= size - 12; i++) {
        size_t header_size, entry_size, count, j;
        unsigned int checksum = 0;

        if (!(i & 15U) && get16(data + i) == 0xaa55) {
            base = i;
            have_base = 1;
        }
        if (get16(data + i) != 0xb8ff || get32(data + i + 2) != 0x544942)
            continue;
        header_size = data[i + 8];
        entry_size = data[i + 9];
        count = data[i + 10];
        if (header_size < 12 || !fits(i, header_size, size))
            return -EBADMSG;
        for (j = 0; j < header_size; j++)
            checksum += data[i + j];
        if (checksum & 255U)
            continue;
        if (++headers > 32)
            return -E2BIG;
        if (entry_size < 6 || !fits(i + header_size, count * entry_size, size))
            return -EBADMSG;
        for (j = 0; j < count; j++) {
            const __u8 *entry = data + i + header_size + j * entry_size;
            size_t directory, general, allocation, frequency, tuning, gating;
            __u32 pointer;
            int err;

            if (entry[0] != 0x6c)
                continue;
            if (have_policy)
                return -EEXIST;
            if (!have_base || get16(entry + 2) < 36)
                return -EBADMSG;
            pointer = entry_size >= 8 ? get32(entry + 4) : get16(entry + 4);
            if (!pointer || !fits(pointer, get16(entry + 2), size - base))
                return -EBADMSG;
            directory = base + pointer;
            general = get32(data + directory);
            allocation = get32(data + directory + 4);
            frequency = get32(data + directory + 8);
            tuning = get32(data + directory + 12);
            gating = get32(data + directory + 16);
            if (!general || !allocation || !frequency || !tuning || !gating ||
                !fits(general, 14, size - base) || !fits(allocation, 8, size - base) ||
                !fits(frequency, 18, size - base) ||
                !fits(tuning, 32, size - base) || !fits(gating, 16, size - base))
                return -EBADMSG;
            frequency += base;
            tuning += base;
            gating += base;
            general += base;
            allocation += base;
            if (data[general] < 0x10 || data[allocation] < 0x10)
                return -EOPNOTSUPP;
            err = n1x_dla_decode_vbios_policy(data + frequency, size - frequency,
                                              data + tuning, size - tuning,
                                              data + gating, size - gating, &parsed);
            if (err)
                return err;
            for (unsigned int k = 0; k < 3; k++)
                parsed.general_header[k] = get16(data + general + 2 * k);
            parsed.feature_mask = get32(data + general + 6);
            parsed.general_word = get32(data + general + 10);
            for (unsigned int k = 0; k < 4; k++)
                parsed.allocation_words[k] = get16(data + allocation + 2 * k);
            parsed.boot_tables_valid = 1;
            found.bit_offset = i;
            found.rom_base = base;
            found.directory_offset = directory;
            found.frequency_offset = frequency;
            found.tuning_offset = tuning;
            found.gating_offset = gating;
            have_policy = 1;
        }
    }
    if (!have_policy)
        return -ENOENT;
    *out = parsed;
    *location = found;
    return 0;
}
