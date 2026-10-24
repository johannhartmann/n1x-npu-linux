/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_firmware.h"

static __u32 get32(const __u8 *p)
{
    return (__u32)p[0] | (__u32)p[1] << 8 | (__u32)p[2] << 16 |
           (__u32)p[3] << 24;
}
static int fits(size_t start, size_t length, size_t end)
{
    return start <= end && length <= end - start;
}
int n1x_dla_parse_firmware(const __u8 *data, size_t size,
                          struct n1x_dla_firmware *out)
{
    struct n1x_dla_firmware parsed;
    __u32 header, total;
    const __u8 *ucode;
    if (!data || !out || size < 24)
        return -EINVAL;
    if (get32(data) != 0x10fe || get32(data + 4) != 1)
        return -EINVAL;
    total = get32(data + 8);
    header = get32(data + 12);
    if (total != size || header < 24 || !fits(header, 0x4c, total))
        return -EINVAL;
    ucode = data + header;
    if (get32(ucode) != 1 && get32(ucode) != 2)
        return -EINVAL;
    parsed.payload_offset = get32(data + 16);
    parsed.payload_size = get32(data + 20);
    parsed.monitor_data_offset = get32(ucode + 0x28);
    parsed.monitor_data_size = get32(ucode + 0x2c);
    parsed.monitor_code_offset = get32(ucode + 0x30);
    parsed.monitor_code_size = get32(ucode + 0x34);
    if (parsed.payload_offset < (size_t)header + 0x4c ||
        !fits(parsed.payload_offset, parsed.payload_size, total) ||
        !fits(parsed.monitor_code_offset, parsed.monitor_code_size,
              parsed.payload_size) ||
        !fits(parsed.monitor_data_offset, parsed.monitor_data_size,
              parsed.payload_size))
        return -EINVAL;
    if (parsed.monitor_code_size && parsed.monitor_data_size &&
        (size_t)parsed.monitor_code_offset <
            (size_t)parsed.monitor_data_offset + parsed.monitor_data_size &&
        (size_t)parsed.monitor_data_offset <
            (size_t)parsed.monitor_code_offset + parsed.monitor_code_size)
        return -EINVAL;
    if ((parsed.monitor_code_offset | parsed.monitor_code_size |
         parsed.monitor_data_offset | parsed.monitor_data_size) & 3)
        return -EINVAL;
    *out = parsed;
    return 0;
}
