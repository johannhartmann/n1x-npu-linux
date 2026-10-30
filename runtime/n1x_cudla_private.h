// SPDX-License-Identifier: MIT
#ifndef N1X_CUDLA_PRIVATE_H
#define N1X_CUDLA_PRIVATE_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Broker-only query for preflighting the kernel's retained-DMA budget.
 * This is not part of NVIDIA's cuDLA ABI and is intentionally not installed
 * as a public header.
 */
int n1x_cudla_materialized_image_size(const void *loadable, size_t loadable_bytes,
                                      uint64_t *image_bytes);
#ifdef __cplusplus
}
#endif
#endif
