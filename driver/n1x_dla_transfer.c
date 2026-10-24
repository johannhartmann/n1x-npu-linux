/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#include "n1x_dla_transfer.h"

/* Windows 617.42 RVA d4bbe0 + verified HAL/accessor chain. */
#define DMACTL 0x10c
#define IMEMC 0x180
#define IMEMD 0x184
#define IMEM_BLOCK_INDEX 0x188
#define DMEMC 0x1c0
#define DMEMD 0x1c4
#define DMEM_BLOCK_INDEX 0x210 /* Observed role; public R38.4 omits this field. */
#define ITFEN 0x48

static int upload_section(const struct n1x_dla_word_io *io,
                          const __u8 *data, __u32 size,
                          __u32 control, __u32 block_index, __u32 port)
{
    __u32 offset, word;
    int err;
    err = io->write32(io->context, control, 1U << 24);
    if (err)
        return err;
    for (offset = 0; offset < size; offset += 4) {
        if (!(offset & 255)) {
            err = io->write32(io->context, block_index, offset >> 8);
            if (err)
                return err;
        }
        word = (__u32)data[offset] | (__u32)data[offset + 1] << 8 |
               (__u32)data[offset + 2] << 16 | (__u32)data[offset + 3] << 24;
        err = io->write32(io->context, port, word);
        if (err)
            return err;
    }
    return 0;
}

int n1x_dla_upload_words(const __u8 *blob, size_t size,
                         __u32 code_limit, __u32 data_limit,
                         const struct n1x_dla_word_io *io)
{
    struct n1x_dla_firmware fw;
    const __u8 *payload;
    int err;
    if (!io || !io->write32)
        return -EINVAL;
    err = n1x_dla_parse_firmware(blob, size, &fw);
    if (err)
        return err;
    if (fw.monitor_code_size > code_limit || fw.monitor_data_size > data_limit)
        return -EFBIG;
    payload = blob + fw.payload_offset;
    err = io->write32(io->context, DMACTL, 0);
    if (err)
        return err;
    err = upload_section(io, payload + fw.monitor_code_offset,
                         fw.monitor_code_size, IMEMC, IMEM_BLOCK_INDEX, IMEMD);
    if (err)
        return err;
    err = upload_section(io, payload + fw.monitor_data_offset,
                         fw.monitor_data_size, DMEMC, DMEM_BLOCK_INDEX, DMEMD);
    if (err)
        return err;
    return io->write32(io->context, ITFEN, 3);
}
