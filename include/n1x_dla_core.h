/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_CORE_H
#define N1X_DLA_CORE_H
#include "n1x_dla_io.h"

int n1x_dla_select_riscv(const struct n1x_dla_io *io);
int n1x_dla_wait_idle(const struct n1x_dla_io *io,
		      __u32 polls, __u32 interval_us);
int n1x_dla_start_core(const struct n1x_dla_io *io,
		       __u32 polls, __u32 interval_us);
#endif
