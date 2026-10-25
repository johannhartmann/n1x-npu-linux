/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/errno.h>
#ifdef __KERNEL__
#include <linux/string.h>
#else
#include <string.h>
#endif
#include "n1x_dla_model.h"

static const struct n1x_dla_content *find_content(
    const struct n1x_dla_content *contents, size_t count,
    const struct n1x_dla_content_init *init)
{
    const struct n1x_dla_content *match = NULL;
    size_t i;

    for (i = 0; i < count; i++) {
        if (contents[i].name_bytes != init->name_bytes ||
            memcmp(contents[i].name, init->name, init->name_bytes))
            continue;
        if (match)
            return NULL; /* Do not choose between ambiguous model names. */
        match = &contents[i];
    }
    return match;
}

int n1x_dla_initialize_contents(
    const struct n1x_dla_host_memory *memories, size_t num_memories,
    const struct n1x_dla_content *contents, size_t num_contents,
    const struct n1x_dla_content_init *initializers, size_t num_initializers)
{
    size_t i;

    if ((num_memories && !memories) || (num_contents && !contents) ||
        (num_initializers && !initializers))
        return -EINVAL;
    for (i = 0; i < num_contents; i++)
        if (!contents[i].name || !contents[i].name_bytes)
            return -EINVAL;
    for (i = 0; i < num_initializers; i++) {
        const struct n1x_dla_content_init *init = &initializers[i];
        const struct n1x_dla_host_memory *memory;
        const struct n1x_dla_content *content;

        if (!init->name || !init->name_bytes || init->memory_index >= num_memories)
            return -EINVAL;
        memory = &memories[init->memory_index];
        content = find_content(contents, num_contents, init);
        if (!content || (content->bytes && !content->data) ||
            !memory->data || !memory->logical_bytes ||
            memory->logical_bytes > memory->mapped_bytes ||
            init->offset > memory->logical_bytes ||
            content->bytes > memory->logical_bytes - init->offset)
            return -EINVAL;
    }
    /* Repeat lookup without allocation. Caller keeps all input views immutable;
     * the first pass has checked every reference, including later memories.
     */
    for (i = 0; i < num_initializers; i++) {
        const struct n1x_dla_content_init *init = &initializers[i];
        const struct n1x_dla_content *content =
            find_content(contents, num_contents, init);

        if (content->bytes)
            memcpy(memories[init->memory_index].data + (size_t)init->offset,
                   content->data, content->bytes);
    }
    return 0;
}
