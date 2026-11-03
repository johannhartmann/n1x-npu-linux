/* SPDX-License-Identifier: MIT */
#ifndef N1X_ORT_H
#define N1X_ORT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct N1xOrtModule N1xOrtModule;

typedef struct N1xOrtTensorDesc {
	char name[88];
	uint64_t size;          /* Total bytes in hardware surface */
	uint32_t n;
	uint32_t c;
	uint32_t h;
	uint32_t w;
	uint8_t data_format;    /* 3 = FEATURE (CHW16), 4 = FEATURE_X8 (CHW8), 5 = FEATURE (CHW32 INT8) */
	uint8_t data_type;      /* 2 = FP16, 4 = INT8 */
	uint8_t data_category;
	uint8_t pixel_format;   /* 36 = FEATURE_X32 */
	uint8_t pixel_mapping;
	uint8_t reserved[3];
	uint32_t stride[8];     /* stride[0]=elem_bytes, stride[1]=line_bytes, stride[2]=surface_bytes */
} N1xOrtTensorDesc;

/* Load an .nvda binary from memory or file path and optionally bind to /dev/accel/accel0 (fd >= 0).
 * Passing fd < 0 opens /dev/accel/accel0 automatically when n1x_ort_module_run_fp32 is called,
 * while still allowing offline descriptor queries and surface packing without hardware present.
 */
int n1x_ort_module_create(const uint8_t *nvda_bytes, size_t nvda_size,
							int accel_fd, N1xOrtModule **out_module,
							char *err_buf, size_t err_buf_len);

int n1x_ort_module_create_from_file(const char *nvda_path, int accel_fd,
									N1xOrtModule **out_module,
									char *err_buf, size_t err_buf_len);

void n1x_ort_module_destroy(N1xOrtModule *module);

size_t n1x_ort_module_num_inputs(const N1xOrtModule *module);
size_t n1x_ort_module_num_outputs(const N1xOrtModule *module);
int n1x_ort_module_get_input_desc(const N1xOrtModule *module, size_t index,
									N1xOrtTensorDesc *out_desc);
int n1x_ort_module_get_output_desc(const N1xOrtModule *module, size_t index,
									N1xOrtTensorDesc *out_desc);

/* Return the count of hardware execution calls made on this module and whether
 * the most recent call used a retained kernel job (n1x_dla_resubmit).
 */
uint64_t n1x_ort_module_run_count(const N1xOrtModule *module);
int n1x_ort_module_used_resubmit(const N1xOrtModule *module);

/* Configure optional per-input / per-output INT8 quantization scales (float32, length 1 or C)
 * and zero-points (int8, length 1 or C). Default scale is 1.0f, zero-point 0.
 */
int n1x_ort_module_set_input_quant(N1xOrtModule *module, size_t index,
									const float *scales, size_t num_scales,
									const int8_t *zero_points, size_t num_zps);

int n1x_ort_module_set_output_quant(N1xOrtModule *module, size_t index,
									const float *scales, size_t num_scales,
									const int8_t *zero_points, size_t num_zps);

/* Vectorized C++ NCHW float32 <-> NPU surface conversion (usable offline or online). */
int n1x_ort_pack_fp32(const N1xOrtTensorDesc *desc,
						const float *nchw_fp32, size_t num_elements,
						const float *scales, size_t num_scales,
						const int8_t *zero_points, size_t num_zps,
						uint8_t *out_surface, size_t out_surface_bytes);

int n1x_ort_unpack_fp32(const N1xOrtTensorDesc *desc,
						const uint8_t *surface, size_t surface_bytes,
						const float *scales, size_t num_scales,
						const int8_t *zero_points, size_t num_zps,
						float *out_nchw_fp32, size_t num_elements);

/* Execute the .nvda module on /dev/accel/accel0 with contiguous NCHW float32 inputs/outputs.
 * Automatically packs inputs into the staging image, uses n1x_dla_submit on first call and
 * n1x_dla_resubmit (retained kernel job) on subsequent calls when N1X_DLA_CAP_REEXEC_IO is
 * supported, reads back outputs, and unpacks to contiguous NCHW float32.
 */
int n1x_ort_module_run_fp32(N1xOrtModule *module,
							const float *const *inputs_fp32, const size_t *input_elem_counts, size_t num_inputs,
							float *const *outputs_fp32, const size_t *output_elem_counts, size_t num_outputs,
							char *err_buf, size_t err_buf_len);

#ifdef __cplusplus
}
#endif

#endif /* N1X_ORT_H */
