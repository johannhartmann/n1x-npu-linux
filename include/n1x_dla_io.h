/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_IO_H
#define N1X_DLA_IO_H
#include "n1x_dla_firmware.h"
/* All offsets are logical. A physical backend must translate and bounds-check
 * them, and establish device power/reset state before invoking these helpers.
 * Read/write callbacks return zero or a negative errno. No backend is provided.
 */
struct n1x_dla_io {
    void *context;
    int (*write32)(void *context, __u32 offset, __u32 value);
    int (*read32)(void *context, __u32 offset, __u32 *value);
    void (*delay_us)(void *context, __u32 interval_us);
};
#endif
