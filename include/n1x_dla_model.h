/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_MODEL_H
#define N1X_DLA_MODEL_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

struct n1x_dla_host_memory {
	__u8 *data;
	size_t mapped_bytes;
	__u64 logical_bytes;
};

struct n1x_dla_content {
	const __u8 *name;
	size_t name_bytes;
	const __u8 *data;
	size_t bytes;
};

struct n1x_dla_content_init {
	const __u8 *name;
	size_t name_bytes;
	__u64 offset;
	__u16 memory_index;
};

/* Populate host memory buffers from named .nvda loadable content blobs. */
int n1x_dla_initialize_contents(
	const struct n1x_dla_host_memory *memories, size_t num_memories,
	const struct n1x_dla_content *contents, size_t num_contents,
	const struct n1x_dla_content_init *initializers, size_t num_initializers);
#endif
