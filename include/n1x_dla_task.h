/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_TASK_H
#define N1X_DLA_TASK_H
#include <linux/types.h>
#ifndef __KERNEL__
#include <stddef.h>
#endif

#define N1X_DLA_TASK_V2_BYTES 60
#define N1X_DLA_FENCED_TASK_V2_BYTES 256
#define N1X_DLA_CONTEXT_HEADER_BYTES 64
#define N1X_DLA_CHECKPOINT_BYTES 32

struct n1x_dla_task_args {
	__u64 next;
	__u64 address_list;
	__u64 timeout;
	__u16 num_addresses;
};

/* Serialize a queue-0 v2 task descriptor header. */
int n1x_dla_build_task_v2(__u8 *out, size_t out_size,
			  const struct n1x_dla_task_args *args);

/* Serialize a 256-byte v2 task descriptor with a terminal WRITE_SEM fence postaction. */
int n1x_dla_build_fenced_task_v2(__u8 *out, size_t out_size,
				 const struct n1x_dla_task_args *args,
				 __u64 fence_address, __u32 fence_value);

/* Serialize the 64-byte HAL-1 preemption context header pointing to a 32-byte checkpoint. */
int n1x_dla_build_context_header(__u8 *out, size_t out_size,
				 __u64 checkpoint, __u8 context_tag);

/* Serialize a fenced v2 task descriptor with preemption context-header and restore flags. */
int n1x_dla_build_context_fenced_task_v2(__u8 *out, size_t out_size,
					 const struct n1x_dla_task_args *args,
					 __u64 fence_address, __u32 fence_value,
					 __u64 context_header, __u8 restore);

/* Serialize a fenced v2 task descriptor with BYPASS_EXEC (flags bit 0 at byte 41). */
int n1x_dla_build_bypass_fenced_task_v2(__u8 *out, size_t out_size,
					const struct n1x_dla_task_args *args,
					__u64 fence_address, __u32 fence_value);

size_t n1x_dla_address_list_bytes(__u16 count);
int n1x_dla_build_address_list(__u8 *out, size_t out_size,
			       const __u64 *addresses, __u16 count);
#endif
