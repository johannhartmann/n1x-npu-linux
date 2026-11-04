// SPDX-License-Identifier: GPL-2.0-only
#include <linux/errno.h>
#include "n1x_dla_core.h"

#define IDLESTATE 0x4c

int n1x_dla_wait_idle(const struct n1x_dla_io *io,
					__u32 polls, __u32 interval_us)
{
	__u32 attempt, value;
	int err;

	if (!io || !io->read32 || !polls || polls > 5001 ||
		(polls > 1 && (!interval_us || !io->delay_us)))
		return -EINVAL;
	for (attempt = 0; attempt < polls; attempt++) {
		err = io->read32(io->context, IDLESTATE, &value);
		if (err)
			return err;
		if (!value)
			return 0;
		if (attempt + 1 < polls)
			io->delay_us(io->context, interval_us);
	}
	return -ETIMEDOUT;
}
