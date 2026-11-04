// SPDX-License-Identifier: GPL-2.0-only
#include <linux/errno.h>
#include <linux/string.h>
#include "n1x_dla_command.h"

#define MTHDID 0x68
#define MTHDWDAT 0x6c
#define MTHD_PENDING (1U << 16)

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

int n1x_dla_send_method(const struct n1x_dla_io *io, __u32 method,
			__u32 data, __u32 polls, __u32 interval_us)
{
	__u32 attempt, value;
	int err;

	if (!io || !io->write32 || !io->read32 || !polls || polls > 5001 ||
	    (polls > 1 && (!interval_us || !io->delay_us)))
		return -EINVAL;
	for (attempt = 0; attempt < polls; attempt++) {
		err = io->read32(io->context, MTHDID, &value);
		if (err)
			return err;
		if (!(value & MTHD_PENDING))
			break;
		if (attempt + 1 == polls)
			return -ETIMEDOUT;
		io->delay_us(io->context, interval_us);
	}
	err = io->write32(io->context, MTHDWDAT, data);
	if (err)
		return err;
	return io->write32(io->context, MTHDID, method);
}

int n1x_dla_build_region(__u8 *out, size_t out_size, __u32 region,
			 __u64 address, __u32 bytes)
{
	__u8 record[16];

	if (!out || out_size < sizeof(record) || (region != 1 && region != 3 && region != 5) ||
	    !address || (address & 3) || !bytes || (region == 5 && bytes < 8) || address > ~(__u64)0 - bytes)
		return -EINVAL;
	/* Packed SET_REGIONS payload for printf (1), trace (3), and HAL-1 ring (5).
	 * The control command points at this descriptor record rather than at address.
	 */
	put32(record, region);
	put64(record + 4, address);
	put32(record + 12, bytes);
	memcpy(out, record, sizeof(record));
	return 0;
}

int n1x_dla_build_command23(__u8 *out, size_t out_size, __u8 context_47d,
			    __u32 context_490, __u32 provider_f4)
{
	__u8 record[40] = {1, context_47d != 0};
	__u32 scaled = context_490 ? provider_f4 * 2000U : 0;

	if (!out || out_size < sizeof(record))
		return -EINVAL;
	put32(record + 2, scaled);
	memcpy(out, record, sizeof(record));
	return 0;
}

int n1x_dla_prepare_default_config(__u8 *buffer, size_t buffer_size)
{
	if (!buffer || buffer_size < N1X_DLA_CONFIG_BYTES)
		return -EINVAL;
	/* Apply the default HAL-1 configuration flags at offsets 0x01 and 0x32
	 * prior to issuing control command 21.
	 */
	buffer[1] = 2;
	buffer[0x32] = 1;
	return 0;
}

int n1x_dla_build_preempt_request(__u8 *out, size_t out_size, __u64 dma,
				  __u64 fence, __u32 submitted, __u32 sequence)
{
	__u8 request[N1X_DLA_PREEMPT_REQUEST_BYTES] = {0};
	__u64 post;

	if (!out || out_size < sizeof(request) || !dma || (dma & 255) ||
	    dma > ((__u64)1 << 36) - sizeof(request) || !fence || (fence & 3) ||
	    fence > ((__u64)1 << 36) - 4 ||
	    (fence < dma + sizeof(request) && fence + 4 > dma))
		return -EINVAL;
	post = dma + 256;
	request[0] = 1;
	/* Store the sequence number at packed offset +3 even when preaction length is zero. */
	put32(request + 3, sequence);
	request[11] = 27;
	request[256] = request[269] = 0x80; /* WRITE_SEM */
	put64(request + 13, post);
	put64(request + 257, fence);
	put64(request + 270, fence);
	put32(request + 265, submitted);
	put32(request + 278, sequence);
	/* Byte 282 is TERM. Both cache blocks belong to this allocation. */
	memcpy(out, request, sizeof(request));
	return 0;
}
