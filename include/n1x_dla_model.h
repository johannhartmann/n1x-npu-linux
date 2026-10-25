/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_MODEL_H
#define N1X_DLA_MODEL_H
#include "n1x_dla_firmware.h"

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

/* Initialize private CPU buffers from named model contents. The caller supplies
 * bounded, immutable table/name/data views and allocator-owned destination
 * extents; this is not a FlatBuffers parser or a userspace-pointer validator.
 * Memory indices refer to the caller's dense, already ID-checked memory table.
 * Initializers must already be selected by the model's initialization flag,
 * in memory/vector order, with name/offset vector lengths checked by the caller.
 * Names are nonempty length-delimited byte strings, not C strings. A selected
 * name must match exactly one content. All catalog names and selected extents
 * are checked before any copy, so invalid input leaves every buffer unchanged.
 * Zero-byte contents are allowed; nonempty contents require a data pointer.
 * Copies preserve order, including overlapping destination ranges. Destinations
 * must not overlap source data, names or metadata; inputs cannot change during
 * the call. Padding and uninitialized memory are preserved, not zeroed here.
 * Version/engine compatibility, instruction validation, DMA visibility, buffer
 * lifetime and publication to a device remain caller responsibilities.
 */
int n1x_dla_initialize_contents(
    const struct n1x_dla_host_memory *memories, size_t num_memories,
    const struct n1x_dla_content *contents, size_t num_contents,
    const struct n1x_dla_content_init *initializers, size_t num_initializers);
#endif
