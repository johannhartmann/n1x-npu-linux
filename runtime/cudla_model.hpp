// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>
#include <vector>
#include "uapi/n1x_dla_accel.h"
#if __has_include(<cudla.h>)
#include <cudla.h>
#else
#define CUDLA_LOADABLE_TENSOR_DESC_NUM_STRIDES 8
struct cudlaModuleTensorDescriptor {
    char name[88];
    uint64_t size;
    uint64_t n, c, h, w;
    uint8_t dataFormat, dataType, dataCategory, pixelFormat, pixelMapping;
    uint32_t stride[CUDLA_LOADABLE_TENSOR_DESC_NUM_STRIDES];
};
#endif

namespace n1x {
struct Tensor {
    cudlaModuleTensorDescriptor desc{};
    uint64_t offset{};
    uint16_t bind_id{};
};
struct Stats {
    cudlaModuleTensorDescriptor desc{};
    uint64_t offset{};
    bool present{};
};
struct Model {
    std::vector<uint8_t> image;
    std::vector<n1x_dla_accel_range> ranges;
    std::vector<Tensor> inputs, outputs;
    Stats stats{};
    // Throws std::invalid_argument on unsupported/malformed NVDA metadata.
    // Instructions remain trusted; the kernel still requires CAP_SYS_RAWIO.
    Model(const uint8_t *bytes, size_t size);
};
}
