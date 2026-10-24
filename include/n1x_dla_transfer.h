/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_TRANSFER_H
#define N1X_DLA_TRANSFER_H
#include "n1x_dla_firmware.h"

struct n1x_dla_word_io {
    void *context;
    int (*write32)(void *context, __u32 offset, __u32 value);
};
/*
 * Firmware upload only, not device initialization or CPU start. Caller must
 * establish the correct resource aperture, power/reset state and memory limits.
 * No register backend or device binding is supplied by this component.
 * write32 must return 0 or a negative errno; its first error stops the upload.
 */
int n1x_dla_upload_words(const __u8 *blob, size_t size,
                         __u32 code_limit, __u32 data_limit,
                         const struct n1x_dla_word_io *io);
#endif
