/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_CORE_H
#define N1X_DLA_CORE_H
#include "n1x_dla_io.h"
/* Selection only; caller controls sequencing with upload and power/reset. */
int n1x_dla_select_riscv(const struct n1x_dla_io *io);
/* Start an already-uploaded core and wait for IDLESTATE=0, not protocol ready.
 * polls must be 1..5001. Multiple polls require a nonzero interval and delay
 * callback; the interval is an explicit Linux policy, not an inferred Windows
 * delay-helper unit. At most polls-1 delays are performed.
 */
int n1x_dla_start_core(const struct n1x_dla_io *io,
                      __u32 polls, __u32 interval_us);
#endif
