/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_SROOT_FIRMWARE_H
#define N1X_SROOT_FIRMWARE_H

#include <linux/types.h>
#ifdef __KERNEL__
#include <linux/errno.h>
#else
#include <errno.h>
#include <stddef.h>
#endif

static inline __u32 n1x_sroot_image_u32(const __u8 *p)
{
	return (__u32)p[0] | (__u32)p[1] << 8 |
	       (__u32)p[2] << 16 | (__u32)p[3] << 24;
}

/* Validate the signed-container envelope and native section bounds. SROOT
 * authenticates the original certificate/signatures; this is not a host
 * signature verifier and does not restrict firmware to a particular build.
 * The caller supplies the loader image ID, excluding the transport tag.
 */
static inline int n1x_sroot_validate_image(const __u8 *image, size_t bytes, __u32 id)
{
	const __u8 *native;
	size_t prefix, table, end = 0;
	__u32 count, i;

	if (!image || bytes < 3 || bytes > ~(__u32)0 - 4U || image[0] != 0x59)
		return -EBADMSG;
	prefix = 3U + ((__u32)image[1] << 8 | image[2]);
	if (prefix > bytes || bytes - prefix < 16)
		return -EBADMSG;
	native = image + prefix;
	bytes -= prefix;
	count = n1x_sroot_image_u32(native + 4);
	if (n1x_sroot_image_u32(native) != 1 || !count || count > (bytes - 16) / 32 ||
	    n1x_sroot_image_u32(native + 8) != 0xefbeadde)
		return -EBADMSG;
	table = 16U + (size_t)count * 32U;
	if (n1x_sroot_image_u32(native + table - 4) != 0xefbeadde)
		return -EBADMSG;
	for (i = 0; i < count; i++) {
		const __u8 *section = native + 12 + i * 32;
		__u32 offset = n1x_sroot_image_u32(section + 12);
		__u32 header = n1x_sroot_image_u32(section + 16);
		__u32 payload = n1x_sroot_image_u32(section + 20);

		if (n1x_sroot_image_u32(section) != 0xefbeadde ||
		    n1x_sroot_image_u32(section + 4) != id || offset != end ||
		    !payload || header > bytes - end || payload > bytes - end - header ||
		    (!i && (header || payload != table)))
			return -EBADMSG;
		end += (size_t)header + payload;
	}
	return end == bytes ? 0 : -EBADMSG;
}

#endif
