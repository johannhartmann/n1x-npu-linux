// SPDX-License-Identifier: MIT
#pragma once
#include <cudla.h>
#include <cstdint>
#include <vector>
#include "uapi/n1x_dla_accel.h"

namespace n1x {
struct Tensor {
    cudlaModuleTensorDescriptor desc{};
    uint64_t offset{};
    uint16_t bind_id{};
};
struct Model {
    std::vector<uint8_t> image;
    std::vector<n1x_dla_accel_range> ranges;
    std::vector<Tensor> inputs, outputs;
    // Throws std::invalid_argument on unsupported/malformed NVDA metadata.
    // Instructions remain trusted; the kernel still requires CAP_SYS_RAWIO.
    Model(const uint8_t *bytes, size_t size);
};
}
