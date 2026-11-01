// SPDX-License-Identifier: MIT
// Native C/C++ surface packer/unpacker and retained-job execution runtime for N1X ONNX EP.
#include "n1x_ort.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifndef N1X_ORT_CLI_MAIN
#include "cudla_model.hpp"
#include "n1x_dla.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <fstream>
#include <glob.h>
#include <memory>
#include <stdexcept>
#include <unistd.h>

struct N1xOrtQuantParams {
    std::vector<float> scales{1.0f};
    std::vector<int8_t> zero_points{0};
};

struct N1xOrtModule {
    n1x::Model model;
    std::vector<uint8_t> staging_image;
    std::vector<N1xOrtTensorDesc> input_descs;
    std::vector<N1xOrtTensorDesc> output_descs;
    std::vector<N1xOrtQuantParams> input_quant;
    std::vector<N1xOrtQuantParams> output_quant;
    int fd = -1;
    bool owns_fd = false;
    uint64_t retained_job_id = 0;
    uint64_t run_count = 0;
    bool last_used_resubmit = false;

    N1xOrtModule(const uint8_t *bytes, size_t size, int accel_fd)
        : model(bytes, size), staging_image(model.image), fd(accel_fd), owns_fd(false) {
        input_descs.resize(model.inputs.size());
        input_quant.resize(model.inputs.size());
        for (size_t i = 0; i < model.inputs.size(); ++i) {
            fill_desc(model.inputs[i].desc, &input_descs[i]);
        }
        output_descs.resize(model.outputs.size());
        output_quant.resize(model.outputs.size());
        for (size_t i = 0; i < model.outputs.size(); ++i) {
            fill_desc(model.outputs[i].desc, &output_descs[i]);
        }
    }

    ~N1xOrtModule() {
        if (owns_fd && fd >= 0) {
            close(fd);
            fd = -1;
        }
    }

    static void fill_desc(const cudlaModuleTensorDescriptor &src, N1xOrtTensorDesc *dst) {
        std::memset(dst, 0, sizeof(*dst));
        std::memcpy(dst->name, src.name, std::min(sizeof(dst->name) - 1, sizeof(src.name)));
        dst->size = src.size;
        dst->n = static_cast<uint32_t>(src.n);
        dst->c = static_cast<uint32_t>(src.c);
        dst->h = static_cast<uint32_t>(src.h);
        dst->w = static_cast<uint32_t>(src.w);
        dst->data_format = src.dataFormat;
        dst->data_type = src.dataType;
        dst->data_category = src.dataCategory;
        dst->pixel_format = src.pixelFormat;
        dst->pixel_mapping = src.pixelMapping;
        for (size_t i = 0; i < 8; ++i) {
            dst->stride[i] = src.stride[i];
        }
    }
};

namespace {

void set_err(char *err_buf, size_t err_buf_len, const char *msg) {
    if (err_buf && err_buf_len > 0) {
        std::snprintf(err_buf, err_buf_len, "%s", msg ? msg : "unknown error");
    }
}

int open_default_accel_device() {
    if (const char *path = std::getenv("N1X_DLA_DEVICE")) {
        return n1x_dla_open(path);
    }
    glob_t paths{};
    if (glob("/dev/accel/accel*", 0, nullptr, &paths) != 0) {
        globfree(&paths);
        return -ENOENT;
    }
    int result = -ENOENT;
    for (size_t i = 0; i < paths.gl_pathc; ++i) {
        int fd = n1x_dla_open(paths.gl_pathv[i]);
        if (fd >= 0) {
            result = fd;
            break;
        }
        if (fd != -ENODEV) {
            result = fd;
        }
    }
    globfree(&paths);
    return result;
}

bool resolve_surface_group(const N1xOrtTensorDesc &desc, uint32_t *out_group_channels, uint32_t *out_elem_bytes) {
    if (desc.n != 1 || desc.c == 0 || desc.h == 0 || desc.w == 0) {
        return false;
    }
    if (desc.data_type == 2 && (desc.data_format == 3 || desc.data_format == 4) && desc.stride[0] == 2) {
        *out_group_channels = (desc.data_format == 4) ? 8U : 16U;
        *out_elem_bytes = 2U;
        return true;
    }
    if (desc.data_type == 4 && (desc.data_format == 3 || desc.data_format == 5) && desc.stride[0] == 1) {
        *out_group_channels = (desc.data_format == 5 || desc.pixel_format == 36) ? 32U : 16U;
        *out_elem_bytes = 1U;
        return true;
    }
    return false;
}

bool validate_surface_strides(const N1xOrtTensorDesc &desc, uint32_t group_channels, uint32_t elem_bytes,
                              uint64_t *out_line, uint64_t *out_surface, uint64_t *out_required) {
    const uint64_t pixel_group_bytes = static_cast<uint64_t>(group_channels) * elem_bytes;
    const uint64_t groups = (static_cast<uint64_t>(desc.c) + group_channels - 1) / group_channels;
    const uint64_t line = desc.stride[1] ? static_cast<uint64_t>(desc.stride[1]) : static_cast<uint64_t>(desc.w) * pixel_group_bytes;
    const uint64_t surface = desc.stride[2] ? static_cast<uint64_t>(desc.stride[2]) : static_cast<uint64_t>(desc.h) * line;
    const uint64_t required =
        (groups - 1) * surface + (static_cast<uint64_t>(desc.h) - 1) * line +
        (static_cast<uint64_t>(desc.w) - 1) * pixel_group_bytes + pixel_group_bytes;
    if (line < static_cast<uint64_t>(desc.w) * pixel_group_bytes ||
        surface < static_cast<uint64_t>(desc.h) * line ||
        required > desc.size) {
        return false;
    }
    *out_line = line;
    *out_surface = surface;
    *out_required = required;
    return true;
}

} // namespace

extern "C" {

int n1x_ort_module_create(const uint8_t *nvda_bytes, size_t nvda_size,
                          int accel_fd, N1xOrtModule **out_module,
                          char *err_buf, size_t err_buf_len) {
    if (!nvda_bytes || !nvda_size || !out_module) {
        set_err(err_buf, err_buf_len, "invalid null argument to n1x_ort_module_create");
        return -EINVAL;
    }
    *out_module = nullptr;
    try {
        auto mod = std::make_unique<N1xOrtModule>(nvda_bytes, nvda_size, accel_fd);
        *out_module = mod.release();
        return 0;
    } catch (const std::invalid_argument &ex) {
        set_err(err_buf, err_buf_len, ex.what());
        return -EINVAL;
    } catch (const std::bad_alloc &) {
        set_err(err_buf, err_buf_len, "out of memory allocating N1xOrtModule");
        return -ENOMEM;
    } catch (const std::exception &ex) {
        set_err(err_buf, err_buf_len, ex.what());
        return -EIO;
    }
}

int n1x_ort_module_create_from_file(const char *nvda_path, int accel_fd,
                                    N1xOrtModule **out_module,
                                    char *err_buf, size_t err_buf_len) {
    if (!nvda_path || !out_module) {
        set_err(err_buf, err_buf_len, "invalid null argument to n1x_ort_module_create_from_file");
        return -EINVAL;
    }
    std::ifstream ifs(nvda_path, std::ios::binary | std::ios::ate);
    if (!ifs) {
        set_err(err_buf, err_buf_len, "failed to open .nvda file");
        return -ENOENT;
    }
    const auto end_pos = ifs.tellg();
    if (end_pos <= 0 || static_cast<uint64_t>(end_pos) > (64U << 20)) {
        set_err(err_buf, err_buf_len, ".nvda file size outside valid bounds");
        return -EINVAL;
    }
    std::vector<uint8_t> bytes(static_cast<size_t>(end_pos));
    ifs.seekg(0, std::ios::beg);
    if (!ifs.read(reinterpret_cast<char *>(bytes.data()), static_cast<std::streamsize>(bytes.size()))) {
        set_err(err_buf, err_buf_len, "failed reading .nvda file");
        return -EIO;
    }
    return n1x_ort_module_create(bytes.data(), bytes.size(), accel_fd, out_module, err_buf, err_buf_len);
}

void n1x_ort_module_destroy(N1xOrtModule *module) {
    delete module;
}

size_t n1x_ort_module_num_inputs(const N1xOrtModule *module) {
    return module ? module->input_descs.size() : 0;
}

size_t n1x_ort_module_num_outputs(const N1xOrtModule *module) {
    return module ? module->output_descs.size() : 0;
}

int n1x_ort_module_get_input_desc(const N1xOrtModule *module, size_t index,
                                  N1xOrtTensorDesc *out_desc) {
    if (!module || !out_desc || index >= module->input_descs.size()) {
        return -EINVAL;
    }
    *out_desc = module->input_descs[index];
    return 0;
}

int n1x_ort_module_get_output_desc(const N1xOrtModule *module, size_t index,
                                   N1xOrtTensorDesc *out_desc) {
    if (!module || !out_desc || index >= module->output_descs.size()) {
        return -EINVAL;
    }
    *out_desc = module->output_descs[index];
    return 0;
}

uint64_t n1x_ort_module_run_count(const N1xOrtModule *module) {
    return module ? module->run_count : 0;
}

int n1x_ort_module_used_resubmit(const N1xOrtModule *module) {
    return (module && module->last_used_resubmit) ? 1 : 0;
}

int n1x_ort_module_set_input_quant(N1xOrtModule *module, size_t index,
                                   const float *scales, size_t num_scales,
                                   const int8_t *zero_points, size_t num_zps) {
    if (!module || index >= module->input_quant.size()) {
        return -EINVAL;
    }
    const uint32_t c = module->input_descs[index].c;
    if (!scales || (num_scales != 1 && num_scales != c)) {
        return -EINVAL;
    }
    for (size_t i = 0; i < num_scales; ++i) {
        if (!std::isfinite(scales[i]) || scales[i] <= 0.0f) {
            return -EINVAL;
        }
    }
    module->input_quant[index].scales.assign(scales, scales + num_scales);
    if (zero_points && (num_zps == 1 || num_zps == c)) {
        module->input_quant[index].zero_points.assign(zero_points, zero_points + num_zps);
    } else {
        module->input_quant[index].zero_points.assign(1, 0);
    }
    return 0;
}

int n1x_ort_module_set_output_quant(N1xOrtModule *module, size_t index,
                                    const float *scales, size_t num_scales,
                                    const int8_t *zero_points, size_t num_zps) {
    if (!module || index >= module->output_quant.size()) {
        return -EINVAL;
    }
    const uint32_t c = module->output_descs[index].c;
    if (!scales || (num_scales != 1 && num_scales != c)) {
        return -EINVAL;
    }
    for (size_t i = 0; i < num_scales; ++i) {
        if (!std::isfinite(scales[i]) || scales[i] <= 0.0f) {
            return -EINVAL;
        }
    }
    module->output_quant[index].scales.assign(scales, scales + num_scales);
    if (zero_points && (num_zps == 1 || num_zps == c)) {
        module->output_quant[index].zero_points.assign(zero_points, zero_points + num_zps);
    } else {
        module->output_quant[index].zero_points.assign(1, 0);
    }
    return 0;
}

int n1x_ort_pack_fp32(const N1xOrtTensorDesc *desc,
                      const float *nchw_fp32, size_t num_elements,
                      const float *scales, size_t num_scales,
                      const int8_t *zero_points, size_t num_zps,
                      uint8_t *out_surface, size_t out_surface_bytes) {
    if (!desc || !nchw_fp32 || !out_surface) {
        return -EINVAL;
    }
    const size_t expected_elems =
        static_cast<size_t>(desc->n) * desc->c * desc->h * desc->w;
    if (num_elements != expected_elems || out_surface_bytes < desc->size) {
        return -EINVAL;
    }
    uint32_t group_channels = 0;
    uint32_t elem_bytes = 0;
    if (!resolve_surface_group(*desc, &group_channels, &elem_bytes)) {
        return -EINVAL;
    }
    uint64_t line = 0, surface = 0, required = 0;
    if (!validate_surface_strides(*desc, group_channels, elem_bytes, &line, &surface, &required)) {
        return -EINVAL;
    }
    std::memset(out_surface, 0, desc->size);
    const uint64_t pixel_group_bytes = static_cast<uint64_t>(group_channels) * elem_bytes;
    const uint32_t C = desc->c;
    const uint32_t H = desc->h;
    const uint32_t W = desc->w;

    if (desc->data_type == 2) {
        for (uint32_t c = 0; c < C; ++c) {
            const uint64_t g = c / group_channels;
            const uint64_t cg = c % group_channels;
            const uint64_t c_base = g * surface + cg * elem_bytes;
            const float *src_ch = nchw_fp32 + static_cast<size_t>(c) * H * W;
            for (uint32_t y = 0; y < H; ++y) {
                const uint64_t row_base = c_base + static_cast<uint64_t>(y) * line;
                const float *src_row = src_ch + static_cast<size_t>(y) * W;
                for (uint32_t x = 0; x < W; ++x) {
                    const float val = src_row[x];
                    if (!std::isfinite(val)) {
                        return -EINVAL;
                    }
                    const _Float16 hval = static_cast<_Float16>(val);
                    if (!std::isfinite(static_cast<float>(hval))) {
                        return -EINVAL;
                    }
                    std::memcpy(out_surface + row_base + static_cast<uint64_t>(x) * pixel_group_bytes,
                                &hval, sizeof(hval));
                }
            }
        }
        return 0;
    }

    if (scales && num_scales != 0 && num_scales != 1 && num_scales != C) {
        return -EINVAL;
    }
    if (zero_points && num_zps != 0 && num_zps != 1 && num_zps != C) {
        return -EINVAL;
    }
    for (uint32_t c = 0; c < C; ++c) {
        const float s = (scales && num_scales > 0) ? (num_scales == 1 ? scales[0] : scales[c]) : 1.0f;
        const int8_t zp = (zero_points && num_zps > 0) ? (num_zps == 1 ? zero_points[0] : zero_points[c]) : 0;
        if (!std::isfinite(s) || s <= 0.0f) {
            return -EINVAL;
        }
        const uint64_t g = c / group_channels;
        const uint64_t cg = c % group_channels;
        const uint64_t c_base = g * surface + cg;
        const float *src_ch = nchw_fp32 + static_cast<size_t>(c) * H * W;
        for (uint32_t y = 0; y < H; ++y) {
            const uint64_t row_base = c_base + static_cast<uint64_t>(y) * line;
            const float *src_row = src_ch + static_cast<size_t>(y) * W;
            for (uint32_t x = 0; x < W; ++x) {
                const float val = src_row[x];
                if (!std::isfinite(val)) {
                    return -EINVAL;
                }
                const float scaled = std::rint(val / s + static_cast<float>(zp));
                const int32_t clamped = std::max<int32_t>(-128, std::min<int32_t>(127, static_cast<int32_t>(scaled)));
                out_surface[row_base + static_cast<uint64_t>(x) * pixel_group_bytes] =
                    static_cast<uint8_t>(static_cast<int8_t>(clamped));
            }
        }
    }
    return 0;
}

int n1x_ort_unpack_fp32(const N1xOrtTensorDesc *desc,
                        const uint8_t *surface, size_t surface_bytes,
                        const float *scales, size_t num_scales,
                        const int8_t *zero_points, size_t num_zps,
                        float *out_nchw_fp32, size_t num_elements) {
    if (!desc || !surface || !out_nchw_fp32) {
        return -EINVAL;
    }
    const size_t expected_elems =
        static_cast<size_t>(desc->n) * desc->c * desc->h * desc->w;
    if (num_elements != expected_elems || surface_bytes < desc->size) {
        return -EINVAL;
    }
    uint32_t group_channels = 0;
    uint32_t elem_bytes = 0;
    if (!resolve_surface_group(*desc, &group_channels, &elem_bytes)) {
        return -EINVAL;
    }
    uint64_t line = 0, surf_stride = 0, required = 0;
    if (!validate_surface_strides(*desc, group_channels, elem_bytes, &line, &surf_stride, &required)) {
        return -EINVAL;
    }
    const uint64_t pixel_group_bytes = static_cast<uint64_t>(group_channels) * elem_bytes;
    const uint32_t C = desc->c;
    const uint32_t H = desc->h;
    const uint32_t W = desc->w;

    if (desc->data_type == 2) {
        for (uint32_t c = 0; c < C; ++c) {
            const uint64_t g = c / group_channels;
            const uint64_t cg = c % group_channels;
            const uint64_t c_base = g * surf_stride + cg * elem_bytes;
            float *dst_ch = out_nchw_fp32 + static_cast<size_t>(c) * H * W;
            for (uint32_t y = 0; y < H; ++y) {
                const uint64_t row_base = c_base + static_cast<uint64_t>(y) * line;
                float *dst_row = dst_ch + static_cast<size_t>(y) * W;
                for (uint32_t x = 0; x < W; ++x) {
                    _Float16 hval;
                    std::memcpy(&hval, surface + row_base + static_cast<uint64_t>(x) * pixel_group_bytes,
                                sizeof(hval));
                    const float fval = static_cast<float>(hval);
                    if (!std::isfinite(fval)) {
                        return -EINVAL;
                    }
                    dst_row[x] = fval;
                }
            }
        }
        return 0;
    }

    if (scales && num_scales != 0 && num_scales != 1 && num_scales != C) {
        return -EINVAL;
    }
    if (zero_points && num_zps != 0 && num_zps != 1 && num_zps != C) {
        return -EINVAL;
    }
    for (uint32_t c = 0; c < C; ++c) {
        const float s = (scales && num_scales > 0) ? (num_scales == 1 ? scales[0] : scales[c]) : 1.0f;
        const int8_t zp = (zero_points && num_zps > 0) ? (num_zps == 1 ? zero_points[0] : zero_points[c]) : 0;
        if (!std::isfinite(s) || s <= 0.0f) {
            return -EINVAL;
        }
        const uint64_t g = c / group_channels;
        const uint64_t cg = c % group_channels;
        const uint64_t c_base = g * surf_stride + cg;
        float *dst_ch = out_nchw_fp32 + static_cast<size_t>(c) * H * W;
        for (uint32_t y = 0; y < H; ++y) {
            const uint64_t row_base = c_base + static_cast<uint64_t>(y) * line;
            float *dst_row = dst_ch + static_cast<size_t>(y) * W;
            for (uint32_t x = 0; x < W; ++x) {
                const int8_t qval = static_cast<int8_t>(
                    surface[row_base + static_cast<uint64_t>(x) * pixel_group_bytes]);
                dst_row[x] = (static_cast<float>(qval) - static_cast<float>(zp)) * s;
            }
        }
    }
    return 0;
}

int n1x_ort_module_run_fp32(N1xOrtModule *module,
                            const float *const *inputs_fp32, const size_t *input_elem_counts, size_t num_inputs,
                            float *const *outputs_fp32, const size_t *output_elem_counts, size_t num_outputs,
                            char *err_buf, size_t err_buf_len) {
    if (!module || !inputs_fp32 || !input_elem_counts || !outputs_fp32 || !output_elem_counts) {
        set_err(err_buf, err_buf_len, "invalid null argument to n1x_ort_module_run_fp32");
        return -EINVAL;
    }
    if (num_inputs != module->input_descs.size() || num_outputs != module->output_descs.size()) {
        set_err(err_buf, err_buf_len, "input/output tensor count mismatch");
        return -EINVAL;
    }
    if (module->fd < 0) {
        int opened = open_default_accel_device();
        if (opened < 0) {
            set_err(err_buf, err_buf_len, "failed to open /dev/accel/accel* device");
            return opened;
        }
        module->fd = opened;
        module->owns_fd = true;
    }

    for (size_t i = 0; i < num_inputs; ++i) {
        const auto &desc = module->input_descs[i];
        const auto &q = module->input_quant[i];
        const auto &t = module->model.inputs[i];
        int rc = n1x_ort_pack_fp32(
            &desc, inputs_fp32[i], input_elem_counts[i],
            q.scales.data(), q.scales.size(),
            q.zero_points.data(), q.zero_points.size(),
            module->staging_image.data() + t.offset, static_cast<size_t>(desc.size));
        if (rc != 0) {
            set_err(err_buf, err_buf_len, "failed packing input tensor to NPU surface");
            return rc;
        }
    }

    for (const auto &t : module->model.outputs) {
        std::memset(module->staging_image.data() + t.offset, 0xff, static_cast<size_t>(t.desc.size));
    }

    n1x_dla_accel_info info{};
    int err = n1x_dla_info(module->fd, &info);
    if (err != 0) {
        set_err(err_buf, err_buf_len, "n1x_dla_info failed");
        return err;
    }

    const bool reuse = (module->retained_job_id != 0) && ((info.capabilities & N1X_DLA_CAP_REEXEC_IO) != 0);
    n1x_dla_accel_exec result{};
    if (reuse) {
        std::vector<n1x_dla_accel_patch> patches;
        patches.reserve(module->model.inputs.size() + module->model.outputs.size());
        for (const auto *tensors : {&module->model.inputs, &module->model.outputs}) {
            for (const auto &tensor : *tensors) {
                patches.push_back({
                    tensor.offset,
                    tensor.desc.size,
                    reinterpret_cast<uintptr_t>(module->staging_image.data() + tensor.offset),
                });
            }
        }
        err = n1x_dla_resubmit(module->fd, module->retained_job_id,
                               patches.data(), static_cast<unsigned int>(patches.size()), &result);
        module->last_used_resubmit = true;
    } else {
        std::vector<n1x_dla_accel_range> writable;
        writable.reserve(module->model.inputs.size() + module->model.outputs.size());
        for (const auto *tensors : {&module->model.inputs, &module->model.outputs}) {
            for (const auto &tensor : *tensors) {
                writable.push_back({tensor.offset, tensor.desc.size});
            }
        }
        err = n1x_dla_submit(module->fd, module->staging_image.data(),
                             static_cast<unsigned int>(module->staging_image.size()),
                             module->model.ranges.data(), static_cast<unsigned int>(module->model.ranges.size()),
                             writable.data(), static_cast<unsigned int>(writable.size()), &result);
        module->last_used_resubmit = false;
    }

    if (err != 0) {
        set_err(err_buf, err_buf_len, reuse ? "n1x_dla_resubmit failed" : "n1x_dla_submit failed");
        return err;
    }

    if (info.capabilities & N1X_DLA_CAP_REEXEC_IO) {
        module->retained_job_id = result.job_id;
    }
    module->run_count += 1;

    for (size_t i = 0; i < num_outputs; ++i) {
        const auto &t = module->model.outputs[i];
        const auto &desc = module->output_descs[i];
        const auto &q = module->output_quant[i];
        err = n1x_dla_read(module->fd, result.job_id, t.offset,
                           module->staging_image.data() + t.offset, t.desc.size);
        if (err != 0) {
            set_err(err_buf, err_buf_len, "n1x_dla_read failed");
            return err;
        }
        err = n1x_ort_unpack_fp32(
            &desc, module->staging_image.data() + t.offset, static_cast<size_t>(desc.size),
            q.scales.data(), q.scales.size(),
            q.zero_points.data(), q.zero_points.size(),
            outputs_fp32[i], output_elem_counts[i]);
        if (err != 0) {
            set_err(err_buf, err_buf_len, "failed unpacking output tensor from NPU surface");
            return err;
        }
    }
    return 0;
}

} // extern "C"
#endif // !N1X_ORT_CLI_MAIN

#ifdef N1X_ORT_CLI_MAIN
int main(int argc, char **argv) {
    if (argc < 3) {
        std::fprintf(stderr, "Usage: %s <--inspect|--run> <model.nvda>\n", argv[0]);
        return 2;
    }
    const std::string mode = argv[1];
    const char *path = argv[2];
    N1xOrtModule *mod = nullptr;
    char err[256]{};
    int rc = n1x_ort_module_create_from_file(path, -1, &mod, err, sizeof(err));
    if (rc != 0 || !mod) {
        std::fprintf(stderr, "Failed loading %s: %s (%d)\n", path, err, rc);
        return 1;
    }
    const size_t n_in = n1x_ort_module_num_inputs(mod);
    const size_t n_out = n1x_ort_module_num_outputs(mod);
    std::printf("Module: %s (inputs=%zu, outputs=%zu)\n", path, n_in, n_out);
    for (size_t i = 0; i < n_in; ++i) {
        N1xOrtTensorDesc d{};
        n1x_ort_module_get_input_desc(mod, i, &d);
        std::printf("  input[%zu] name=%s shape=[%u,%u,%u,%u] bytes=%llu format=%u type=%u strides=[%u,%u,%u]\n",
                    i, d.name, d.n, d.c, d.h, d.w,
                    static_cast<unsigned long long>(d.size), d.data_format, d.data_type,
                    d.stride[0], d.stride[1], d.stride[2]);
    }
    for (size_t i = 0; i < n_out; ++i) {
        N1xOrtTensorDesc d{};
        n1x_ort_module_get_output_desc(mod, i, &d);
        std::printf("  output[%zu] name=%s shape=[%u,%u,%u,%u] bytes=%llu format=%u type=%u strides=[%u,%u,%u]\n",
                    i, d.name, d.n, d.c, d.h, d.w,
                    static_cast<unsigned long long>(d.size), d.data_format, d.data_type,
                    d.stride[0], d.stride[1], d.stride[2]);
    }
    if (mode == "--run") {
        std::vector<std::vector<float>> in_bufs(n_in);
        std::vector<const float *> in_ptrs(n_in);
        std::vector<size_t> in_counts(n_in);
        for (size_t i = 0; i < n_in; ++i) {
            N1xOrtTensorDesc d{};
            n1x_ort_module_get_input_desc(mod, i, &d);
            in_counts[i] = static_cast<size_t>(d.n) * d.c * d.h * d.w;
            in_bufs[i].assign(in_counts[i], 0.25f);
            in_ptrs[i] = in_bufs[i].data();
        }
        std::vector<std::vector<float>> out_bufs(n_out);
        std::vector<float *> out_ptrs(n_out);
        std::vector<size_t> out_counts(n_out);
        for (size_t i = 0; i < n_out; ++i) {
            N1xOrtTensorDesc d{};
            n1x_ort_module_get_output_desc(mod, i, &d);
            out_counts[i] = static_cast<size_t>(d.n) * d.c * d.h * d.w;
            out_bufs[i].assign(out_counts[i], 0.0f);
            out_ptrs[i] = out_bufs[i].data();
        }
        rc = n1x_ort_module_run_fp32(mod, in_ptrs.data(), in_counts.data(), n_in,
                                     out_ptrs.data(), out_counts.data(), n_out, err, sizeof(err));
        if (rc != 0) {
            std::fprintf(stderr, "Run failed: %s (%d)\n", err, rc);
            n1x_ort_module_destroy(mod);
            return 1;
        }
        std::printf("Run succeeded (out[0][0]=%.6f)\n", out_bufs[0][0]);
    }
    n1x_ort_module_destroy(mod);
    return 0;
}
#endif
