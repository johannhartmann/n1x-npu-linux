# Public & Internal C/C++ Headers (`include/`)

## Purpose

Defines the public userspace-to-kernel UAPI contracts (`/dev/accel/accel0`), the native `t254` ahead-of-time compiler C ABI, the C-backed heterogeneous ONNX Execution Provider C ABI, and the internal kernel driver structures for the NVIDIA N1X (`NVDA200A` / `NVDA8600`) NPU stack.

## Key Concepts

- **Kernel UAPI (`uapi/n1x_dla_accel.h`)**: Fixed-width `ioctl` payload structures (`n1x_dla_accel_info`, `n1x_dla_accel_exec`, `n1x_dla_accel_reexec`, `n1x_dla_accel_read`, `n1x_dla_accel_stats`, `n1x_dla_accel_control`), capability bitmasks (`N1X_DLA_CAP_REEXEC_IO`, `N1X_DLA_CAP_CONTROLLED_STOP`, `N1X_DLA_CAP_STATISTICS`, `N1X_DLA_CAP_PREEMPT`, `N1X_DLA_CAP_MMAP`), and zero-copy read-only `mmap()` offset encoding (`job_id << PAGE_SHIFT`).
- **Native Compiler C ABI (`n1x_compiler.h`)**: Declares the NVIDIA `libnvdla_compiler.so` (`t254`) C/C++ vtable interfaces (`INvDlaCompiler`, `INvDlaNetwork`, `NvDlaTensor`, `Weights`, `QuantizationParams`) and the JSON DAG compilation entrypoint (`n1x_compile_json_graph`).
- **Heterogeneous ONNX EP C ABI (`n1x_ort.h`)**: Declares the opaque `N1xOrtModule` handle, `N1xOrtTensorDesc` surface descriptor, vectorized `FP32` $\leftrightarrow$ `CHW16`/`CHW32` (`FP16`/`INT8`) surface packers (`n1x_ort_pack_fp32`, `n1x_ort_unpack_fp32`), and retained-job execution (`n1x_ort_module_run_fp32`).
- **Driver & HFRP Protocol Headers (`n1x_*.h`)**: Internal kernel structures for Host-to-Firmware Request Protocol (`n1x_hfrp.h`), `SROOT` noncoherent DMA domains (`n1x_sroot.h`), VBIOS frequency/clock tables (`n1x_dla_vbios.h`), and DLA command/ring/task state machines.

## Boundaries & Non-Responsibilities

- **Does Not Do**: Contains no executable implementation logic; kernel implementations reside in [`driver/`](../driver/README.md) and userspace shared libraries reside in [`runtime/`](../runtime/README.md).

## Public Interface / Exports

- [`uapi/n1x_dla_accel.h`](uapi/n1x_dla_accel.h)
  - `DRM_IOCTL_N1X_DLA_INFO`, `DRM_IOCTL_N1X_DLA_EXEC`, `DRM_IOCTL_N1X_DLA_READ`, `DRM_IOCTL_N1X_DLA_REEXEC`, `DRM_IOCTL_N1X_DLA_STATS`, `DRM_IOCTL_N1X_DLA_CONTROL`: Userspace `/dev/accel/accel0` `ioctl` commands.
- [`n1x_compiler.h`](n1x_compiler.h)
  - `n1x_compiler_init(compiler_lib_path, cudla_lib_path) -> int`: Initializes the `t254` compiler symbol table.
  - `n1x_compile_json_graph(json_spec, out_buf, out_size, err_buf, err_cap) -> int`: Compiles a JSON DAG into an `.nvda` FlatBuffers loadable in memory.
  - `n1x_compiler_free_buffer(buf) -> void`: Releases a compiled `.nvda` buffer.
- [`n1x_ort.h`](n1x_ort.h)
  - `n1x_ort_module_create(nvda_bytes, nvda_size, err_buf, err_cap) -> N1xOrtModule*`: Parses an `.nvda` loadable and prepares aligned DMA staging memory.
  - `n1x_ort_pack_fp32(src_fp32, desc, dst_surface, channel_scales, num_scales) -> int`: Packs contiguous `NCHW` `float32` into DLA `CHW16` (`FP16`) or `CHW32` (`INT8`) surfaces.
  - `n1x_ort_unpack_fp32(src_surface, desc, dst_fp32, channel_scales, num_scales) -> int`: Unpacks DLA `CHW16` (`FP16`) or `CHW32` (`INT8`) surfaces into contiguous `NCHW` `float32`.
  - `n1x_ort_module_run_fp32(mod, accel_fd, inputs_fp32, num_inputs, outputs_fp32, num_outputs, err_buf, err_cap) -> int`: Executes a compiled `.nvda` module on `/dev/accel/accel0` using `EXEC` on initial run and `REEXEC_IO` on subsequent runs.
- [`n1x_hfrp.h`](n1x_hfrp.h), [`n1x_sroot.h`](n1x_sroot.h), [`n1x_dla_vbios.h`](n1x_dla_vbios.h), [`n1x_dla_task.h`](n1x_dla_task.h), [`n1x_dla_job.h`](n1x_dla_job.h)
  - Kernel driver internal protocol, DMA, VBIOS clock (`892.8 MHz`–`1500 MHz`), and task submission definitions.

## Dependencies & Consumers

- **Internal Dependencies**: Standard Linux UAPI headers (`<linux/ioctl.h>`, `<linux/types.h>`, `<drm/drm.h>`) and C/C++ standard fixed-width integer headers (`<stdint.h>`, `<stddef.h>`).
- **Primary Consumers**: [`driver/`](../driver/README.md) (`n1x_npu_bootstrap.ko`) and [`runtime/`](../runtime/README.md) (`libn1x_dla.so`, `libn1x_compiler.so`, `libn1x_ort.so`, `libnvcudla.so`).

## Operational Contracts & Invariants

- **Fixed-Width ABI Stability**: All structures in [`uapi/n1x_dla_accel.h`](uapi/n1x_dla_accel.h) and [`n1x_ort.h`](n1x_ort.h) use explicit fixed-width types and reserved padding fields to guarantee identical layout across C11, C++17, and Python `ctypes`.
- **ARM64 `x18` Register Discipline**: Functions invoking Windows ARM64 PE binaries through [`n1x_compiler.h`](n1x_compiler.h) must be compiled with `-ffixed-x18` so the Windows TEB register (`x18`) is never clobbered.
