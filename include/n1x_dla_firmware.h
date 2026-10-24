/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_FIRMWARE_H
#define N1X_DLA_FIRMWARE_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif
struct n1x_dla_firmware {
    __u32 payload_offset, payload_size;
    __u32 monitor_code_offset, monitor_code_size;
    __u32 monitor_data_offset, monitor_data_size;
};
/* Validation only. No memory mapping, device binding or hardware writes. */
int n1x_dla_parse_firmware(const __u8 *data, size_t size,
                          struct n1x_dla_firmware *out);
#endif
