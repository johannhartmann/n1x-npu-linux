// SPDX-License-Identifier: GPL-2.0-only
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_dla_task.h"

static void put16(__u8 *out, __u16 value)
{
	out[0] = value;
	out[1] = value >> 8;
}

static void put32(__u8 *out, __u32 value)
{
	out[0] = value;
	out[1] = value >> 8;
	out[2] = value >> 16;
	out[3] = value >> 24;
}

static void put64(__u8 *out, __u64 value)
{
	put32(out, (__u32)value);
	put32(out + 4, (__u32)(value >> 32));
}

int n1x_dla_build_task_v2(__u8 *out, size_t out_size,
						const struct n1x_dla_task_args *args)
{
	__u8 descriptor[N1X_DLA_TASK_V2_BYTES] = {0};

	/* Copy parameters before writing so overlapping input/output is harmless. */
	if (!out || !args || out_size < sizeof(descriptor) ||
		(args->next & 255) || !args->address_list ||
		(args->address_list & 255) || !args->num_addresses)
		return -EINVAL;
	/* L4T R38.4 dla_task_descriptor: packed, aligned(4), version 2.
	 * Both HAL-1 RV32 and T35x RV64 firmware verify version at 8, engine at 9,
	 * queue at 20 and next-pointer alignment, and copies a 60-byte header.
	 */
	put64(descriptor, args->next);
	descriptor[8] = 2;
	descriptor[9] = 0x44;
	put16(descriptor + 10, sizeof(descriptor));
	put64(descriptor + 21, args->address_list);
	put16(descriptor + 29, args->num_addresses);
	put64(descriptor + 33, args->timeout);
	memcpy(out, descriptor, sizeof(descriptor));
	return 0;
}

int n1x_dla_build_fenced_task_v2(__u8 *out, size_t out_size,
								const struct n1x_dla_task_args *args,
								__u64 fence_address, __u32 fence_value)
{
	__u8 descriptor[N1X_DLA_FENCED_TASK_V2_BYTES] = {0};
	int err;

	if (!out || out_size < sizeof(descriptor) || !fence_address ||
		(fence_address & 3))
		return -EINVAL;
	err = n1x_dla_build_task_v2(descriptor, sizeof(descriptor), args);
	if (err)
		return err;
	put16(descriptor + 10, 96); /* Logical descriptor size, excluding cache padding. */
	descriptor[15] = 1; /* One postaction list; no preactions. */
	put16(descriptor + 18, 60);
	put16(descriptor + 60, 64); /* List offset relative to descriptor base. */
	put16(descriptor + 62, 14); /* Opcode + u64 address + u32 value + TERM. */
	descriptor[64] = 0x80; /* WRITE_SEM */
	put64(descriptor + 65, fence_address);
	put32(descriptor + 73, fence_value);
	/* Byte 77 terminates the stream. Padding through byte 255 covers HAL-1
	 * aligned 256-byte cache fetches (and T35x 16-byte lookahead reads).
	 */
	memcpy(out, descriptor, sizeof(descriptor));
	return 0;
}

int n1x_dla_build_bypass_fenced_task_v2(__u8 *out, size_t out_size,
										const struct n1x_dla_task_args *args,
										__u64 fence_address, __u32 fence_value)
{
	__u8 descriptor[N1X_DLA_FENCED_TASK_V2_BYTES];
	int err;

	/* Single-task BYPASS_EXEC descriptor (no task chaining). */
	if (!out || !args || out_size < sizeof(descriptor) || args->next)
		return -EINVAL;
	err = n1x_dla_build_fenced_task_v2(descriptor, sizeof(descriptor), args,
									fence_address, fence_value);
	if (err)
		return err;
	descriptor[41] = 1; /* Recovered HAL-1 flag / L4T DLA_DESC_FLAGS_BYPASS_EXEC. */
	memcpy(out, descriptor, sizeof(descriptor));
	return 0;
}

int n1x_dla_build_context_header(__u8 *out, size_t out_size,
								__u64 checkpoint, __u8 context_tag)
{
	__u8 header[N1X_DLA_CONTEXT_HEADER_BYTES] = {0};

	if (!out || out_size < sizeof(header) || !checkpoint ||
		(checkpoint & 255) || checkpoint > ((__u64)1 << 36) - 256)
		return -EINVAL;
	header[0] = 1;
	header[1] = context_tag;
	put64(header + 4, checkpoint);
	memcpy(out, header, sizeof(header));
	return 0;
}

int n1x_dla_build_context_fenced_task_v2(__u8 *out, size_t out_size,
										const struct n1x_dla_task_args *args,
										__u64 fence_address, __u32 fence_value,
										__u64 context_header, __u8 restore)
{
	__u8 descriptor[N1X_DLA_FENCED_TASK_V2_BYTES];
	int err;

	if (!out || !args || out_size < sizeof(descriptor) || args->next ||
		args->num_addresses > 512 ||
		args->address_list > ((__u64)1 << 36) - n1x_dla_address_list_bytes(args->num_addresses) ||
		fence_address > ((__u64)1 << 36) - 4 || !context_header ||
		(context_header & 255) || context_header > ((__u64)1 << 36) - 256 || restore > 1)
		return -EINVAL;
	err = n1x_dla_build_fenced_task_v2(descriptor, sizeof(descriptor), args,
									fence_address, fence_value);
	if (err)
		return err;
	/* 1046ee loads the context header only with bit 2 set. The consumer
	 * tests bit 1 before reading the saved-work checkpoint for restoration.
	 */
	put16(descriptor + 41, 4 | (restore ? 2 : 0));
	put64(descriptor + 51, context_header);
	memcpy(out, descriptor, sizeof(descriptor));
	return 0;
}

size_t n1x_dla_address_list_bytes(__u16 count)
{
	if (!count || count > 512)
		return 0;
	return ((size_t)count * 8 + 255) & ~(size_t)255;
}

int n1x_dla_build_address_list(__u8 *out, size_t out_size,
								const __u64 *addresses, __u16 count)
{
	size_t bytes = n1x_dla_address_list_bytes(count);
	unsigned int i;

	if (!out || !addresses || !bytes || out_size < bytes)
		return -EINVAL;
	for (i = 0; i < count; i++)
		put64(out + i * 8, addresses[i]);
	memset(out + (size_t)count * 8, 0, bytes - (size_t)count * 8);
	return 0;
}
