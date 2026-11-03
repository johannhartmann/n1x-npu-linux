/* SPDX-License-Identifier: MIT */
#ifndef N1X_COMPILER_H
#define N1X_COMPILER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void *N1xDlaHandle;

typedef struct {
	int32_t n;
	int32_t c;
	int32_t h;
	int32_t w;
} N1xDlaDims4;

typedef struct {
	int32_t h;
	int32_t w;
} N1xDlaDims2;

typedef struct {
	uint8_t dtype;
	uint8_t pad[7];
	const void *values;
	int64_t count;
} N1xDlaWeights;

enum N1xDlaDataType {
	N1X_DLA_DATA_TYPE_UNKNOWN = 0,
	N1X_DLA_DATA_TYPE_FLOAT = 1,
	N1X_DLA_DATA_TYPE_HALF = 2,
	N1X_DLA_DATA_TYPE_INT16 = 3,
	N1X_DLA_DATA_TYPE_INT8 = 4,
	N1X_DLA_DATA_TYPE_UINT8 = 5,
	N1X_DLA_DATA_TYPE_UINT16 = 6,
};

enum N1xDlaDataFormat {
	N1X_DLA_DATA_FORMAT_UNKNOWN = 0,
	N1X_DLA_DATA_FORMAT_NCHW = 1,
	N1X_DLA_DATA_FORMAT_NHWC = 2,
	N1X_DLA_DATA_FORMAT_NCxHWx = 3,
};

enum N1xDlaActivationType {
	N1X_DLA_ACTIVATION_RELU = 0,
	N1X_DLA_ACTIVATION_SIGMOID = 1,
	N1X_DLA_ACTIVATION_TANH = 2,
	N1X_DLA_ACTIVATION_CLIP = 5,
	N1X_DLA_ACTIVATION_LEAKY_RELU = 6,
};

enum N1xDlaPoolingType {
	N1X_DLA_POOLING_MIN = 0,
	N1X_DLA_POOLING_MAX = 1,
	N1X_DLA_POOLING_AVERAGE = 2,
};

enum N1xDlaElementWiseOperation {
	N1X_DLA_EW_SUM = 0,
	N1X_DLA_EW_PROD = 1,
	N1X_DLA_EW_MIN = 2,
	N1X_DLA_EW_MAX = 3,
	N1X_DLA_EW_SUB = 4,
};

enum N1xDlaScaleMode {
	N1X_DLA_SCALE_NONE = 0,
	N1X_DLA_SCALE_UNIFORM = 1,
	N1X_DLA_SCALE_CHANNEL = 2,
	N1X_DLA_SCALE_ELEMENTWISE = 3,
};

enum N1xDlaBatchNormMode {
	N1X_DLA_BN_UNIFORM = 0,
	N1X_DLA_BN_CHANNEL = 1,
};

enum N1xDlaBiasMode {
	N1X_DLA_BIAS_NONE = 0,
	N1X_DLA_BIAS_UNIFORM = 1,
	N1X_DLA_BIAS_CHANNEL = 2,
	N1X_DLA_BIAS_ELEMENTWISE = 3,
};

enum N1xDlaQuantizationMode {
	N1X_DLA_QUANTIZATION_PER_KERNEL = 0,
	N1X_DLA_QUANTIZATION_PER_FILTER = 1,
};

enum N1xDlaTensorScalingMode {
	N1X_DLA_TENSOR_SCALING_PER_TENSOR = 0,
	N1X_DLA_TENSOR_SCALING_PER_CHANNEL = 1,
};

enum N1xDlaResizeMode {
	N1X_DLA_RESIZE_NEAREST = 0,
	N1X_DLA_RESIZE_LINEAR = 1,
};

/* Initialize the native N1X t254 compiler shim (loads libnvdla_compiler.so and
 * grafts the Windows ARM64 Mtl3 backend in-process). Idempotent and thread-safe.
 * Returns 0 on success, non-zero on error.
 */
int n1x_compiler_init(const char *linux_so_path, const char *windows_dll_path);

/* Compile a JSON graph specification into a serialized FlatBuffers .nvda loadable.
 * Allocates *out_data via malloc(); caller frees with n1x_compiler_free_buffer().
 * Returns 0 on success, non-zero on error.
 */
int n1x_compile_json_graph(const char *json_spec,
							uint8_t **out_data,
							size_t *out_size,
							char *err_buf,
							size_t err_buf_size);

void n1x_compiler_free_buffer(void *ptr);

/* Direct C API bindings into the loaded libnvdla_compiler.so instance. */
uint32_t NvDlaCreateNetwork(N1xDlaHandle *out_network);
void NvDlaDestroyNetwork(N1xDlaHandle network);
uint32_t NvDlaCreateWisdom(N1xDlaHandle *out_wisdom);
void NvDlaDestroyWisdom(N1xDlaHandle wisdom);

uint32_t NvDlaWisdomOpen(N1xDlaHandle wisdom, const char *path);
uint32_t NvDlaWisdomClose(N1xDlaHandle wisdom);
uint32_t NvDlaWisdomSetDataType(N1xDlaHandle wisdom, uint8_t dtype);
uint32_t NvDlaWisdomSetNetworkTransient(N1xDlaHandle wisdom, N1xDlaHandle network);
uint32_t NvDlaWisdomGetCompiler(N1xDlaHandle wisdom, N1xDlaHandle *out_compiler);
uint32_t NvDlaWisdomGetProfiler(N1xDlaHandle wisdom, N1xDlaHandle *out_profiler);

uint32_t NvDlaProfilerCreateProfile(N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_profile);
uint32_t NvDlaProfilerGetProfile(N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_profile);
uint32_t NvDlaProfilerGetTargetConfig(N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_target);
uint32_t NvDlaTargetConfigGetName(N1xDlaHandle target, const char **out_name);

uint32_t NvDlaProfileInitWithDefaultProfile(N1xDlaHandle profile);
uint32_t NvDlaProfileSetComputePrecision(N1xDlaHandle profile, uint8_t precision);
uint32_t NvDlaProfileSetNetworkInputDataFormat(N1xDlaHandle profile, uint8_t format);
uint32_t NvDlaProfileSetNetworkOutputDataFormat(N1xDlaHandle profile, uint8_t format);
uint32_t NvDlaProfileSetNetworkInputSurfaceFormat(N1xDlaHandle profile, uint8_t format);
uint32_t NvDlaProfileSetNetworkOutputSurfaceFormat(N1xDlaHandle profile, uint8_t format);
uint32_t NvDlaProfileSetTensorScalingMode(N1xDlaHandle profile, uint8_t mode);
uint32_t NvDlaProfileSetQuantizationMode(N1xDlaHandle profile, uint8_t mode);
uint32_t NvDlaProfileSetIsExplicitQuantization(N1xDlaHandle profile, uint8_t is_explicit);

uint32_t NvDlaCompilerCompile(N1xDlaHandle compiler, const char *profile_name, const char *target_name, N1xDlaHandle *out_loadable);
uint32_t NvDlaCompilerGetLoadableImageSize(N1xDlaHandle compiler, const char *profile_name, uint64_t *out_size);
uint32_t NvDlaCompilerGetLoadableImage(N1xDlaHandle compiler, const char *profile_name, void *out_buf);

uint32_t NvDlaNetworkAddInput(N1xDlaHandle net, const char *name, N1xDlaDims4 dims, N1xDlaHandle *out_tensor);
uint32_t NvDlaNetworkMarkOutput(N1xDlaHandle net, N1xDlaHandle tensor);
uint32_t NvDlaNetworkGetNumInputs(N1xDlaHandle net, uint32_t *out_count);
uint32_t NvDlaNetworkGetNumOutputs(N1xDlaHandle net, uint32_t *out_count);
uint32_t NvDlaNetworkGetNumLayers(N1xDlaHandle net, uint32_t *out_count);

uint32_t NvDlaNetworkAddConvolution(
	N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs, int32_t pad_val,
	N1xDlaDims2 kernel, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaDims2 stride, N1xDlaDims2 dilation,
	N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, int32_t num_groups, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddDeconvolution(
	N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs, int32_t pad_val,
	N1xDlaDims2 kernel, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaDims2 stride, N1xDlaDims2 dilation,
	N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, int32_t num_groups, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddFullyConnected(
	N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs,
	N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddActivation(N1xDlaHandle net, N1xDlaHandle input, uint8_t act_type, N1xDlaHandle *out_layer);
uint32_t NvDlaNetworkAddActivationWeights(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t act_type,
	N1xDlaWeights alpha, N1xDlaWeights beta, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddPooling(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t pool_type,
	N1xDlaDims2 window, N1xDlaDims2 stride, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddElementWise(
	N1xDlaHandle net, N1xDlaHandle input0, N1xDlaHandle input1, uint8_t op_type, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddScale(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t mode,
	N1xDlaWeights shift, N1xDlaWeights scale, N1xDlaWeights power, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddBatchNorm(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t mode,
	N1xDlaWeights mean, N1xDlaWeights variance, float epsilon, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddConcatenation(
	N1xDlaHandle net, const N1xDlaHandle *inputs, int32_t num_inputs, N1xDlaHandle *out_layer);
uint32_t NvDlaConcatenationLayerSetAxis(N1xDlaHandle layer, int32_t axis);

uint32_t NvDlaNetworkAddSlice(
	N1xDlaHandle net, N1xDlaHandle input,
	N1xDlaWeights start, N1xDlaWeights size, N1xDlaWeights stride,
	N1xDlaWeights fill_val, uint8_t mode, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddPixelShuffle(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t type, int32_t upscale_factor, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddResize(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t mode, N1xDlaWeights scales, N1xDlaHandle *out_layer);
uint32_t NvDlaNetworkAddResizeOutputDims(
	N1xDlaHandle net, N1xDlaHandle input, uint8_t mode, N1xDlaDims4 out_dims, uint8_t align_corners, N1xDlaHandle *out_layer);

uint32_t NvDlaNetworkAddQuantize(
	N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights scales, N1xDlaWeights zero_points, int32_t axis, N1xDlaHandle *out_layer);
uint32_t NvDlaNetworkAddDequantize(
	N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights scales, N1xDlaWeights zero_points, int32_t axis, N1xDlaHandle *out_layer);

uint32_t NvDlaLayerGetOutput(N1xDlaHandle layer, int32_t index, N1xDlaHandle *out_tensor);
uint32_t NvDlaLayerGetInput(N1xDlaHandle layer, int32_t index, N1xDlaHandle *out_tensor);
uint32_t NvDlaLayerSetName(N1xDlaHandle layer, const char *name);
uint32_t NvDlaLayerSetComputePrecision(N1xDlaHandle layer, uint8_t precision);

uint32_t NvDlaTensorSetName(N1xDlaHandle tensor, const char *name);
uint32_t NvDlaTensorGetDimensions(N1xDlaHandle tensor, N1xDlaDims4 *out_dims);
uint32_t NvDlaTensorSetDataType(N1xDlaHandle tensor, uint8_t dtype);
uint32_t NvDlaTensorGetDataType(N1xDlaHandle tensor, uint8_t *out_dtype);
uint32_t NvDlaTensorSetDataFormat(N1xDlaHandle tensor, uint8_t format);
uint32_t NvDlaTensorSetSurfaceFormat(N1xDlaHandle tensor, uint8_t format);
uint32_t NvDlaTensorSetChannelDynamicRange(N1xDlaHandle tensor, int32_t channel_idx, float min_val, float max_val);
uint32_t NvDlaTensorSetChannelOffset(N1xDlaHandle tensor, int32_t channel_idx, float offset);

#ifdef __cplusplus
}
#endif

#endif /* N1X_COMPILER_H */
