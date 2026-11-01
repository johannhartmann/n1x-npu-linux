# Userspace N1X NPU Runtime, Native Compiler & ONNX Execution Provider (`runtime/`)

## Purpose

Provides the userspace C/C++ shared libraries, CLI binaries, and Python modules for:
1. Low-level `/dev/accel/accel0` transport (`libn1x_dla.so`, `n1x_dla.py`) and `.nvda` FlatBuffers loadable materialization (`cudla_model.cpp`, `loadable.py`).
2. Reconstructed Linux cuDLA 13.4.92 backend (`cudla_backend.cpp` $\rightarrow$ `cudla/libnvcudla.so`) and root-isolated IPC broker (`cudla_broker.cpp`, `cudla_broker.py`).
3. Native in-process `t254` ahead-of-time neural network compiler (`libn1x_compiler.so`, `n1x-compile`, `n1x_compiler.py`) supporting `FP16` and `INT8` (per-tensor and per-channel) multi-branch DAGs.
4. C-backed heterogeneous ONNX Execution Provider and graph partitioner (`libn1x_ort.so`, `n1x-ort-run`, `n1x_ort.py`) providing an `onnxruntime.InferenceSession`-compatible API (`N1xInferenceSession` / `InferenceSession`).

## Key Concepts

- **`.nvda` Loadable Materialization (`cudla_model.cpp`, `loadable.py`)**: Parses FlatBuffers `.nvda` loadables, allocates 4096-byte page-aligned DMA images, populates the self-referential `address_list` (`entries[0].size = 0`) and task descriptors, and computes writable input/output surface ranges for `REEXEC_IO` reuse.
- **Native `t254` Compiler (`n1x_compiler_shim.cpp`, `n1x_compile.cpp`, `n1x_compiler.py`)**: Loads `libnvdla_compiler.so` (`t254` target) in-process via an ARM64 Windows-to-Linux PE loader (`-ffixed-x18`) and compiles JSON DAGs or ONNX subgraphs (`Conv`, `Gemm`/`FullyConnected`, `Relu`, `Clip`, `MaxPool`, `AveragePool`, `GlobalAveragePool`, `Add`, `Sub`, `Mul`, `Concat`, `BatchNormalization`, `Scale`, `Sigmoid`, `Tanh`) in `FP16` or `INT8`.
- **C-Backed Surface Packer & Retained-Job Executor (`n1x_ort.cpp`)**: Implements vectorized C++17 `NCHW` `float32` $\leftrightarrow$ `CHW16` (`FP16` `_Float16`) and `CHW32` (`INT8`) tensor packing/unpacking and executes `.nvda` loadables via `n1x_dla_submit` (initial run) and `n1x_dla_resubmit` (`REEXEC_IO` fast path on subsequent runs).
- **Heterogeneous ONNX Graph Partitioner (`n1x_ort.py`)**: Canonicalizes ONNX graphs (`auto_pad` resolution, `Conv+Add` fusion, `Flatten/Reshape+MatMul+Add` $\rightarrow$ `Gemm`, opset-10 `Reshape->Transpose->Reshape` $\rightarrow$ `DepthToSpace`), collects multi-sample `INT8` calibration ranges, partitions graphs into cycle-free topological `DLAStage` and `HostStage` segments with SHA-256 disk caching (`~/.cache/n1x/ort/`), and orchestrates end-to-end execution.

## Boundaries & Non-Responsibilities

- **Does Not Do**: Does not manage kernel HFRP power/clock registers or IOMMU group bindings directly (handled by [`driver/`](../driver/README.md) and [`scripts/npu_autostart.py`](../scripts/npu_autostart.py)).
- **Does Not Do**: Does not execute unsupported non-DLA ONNX operators (`DepthToSpace`, `ReduceMean`, `Resize`, `Softmax`, dynamic `Reshape`/`Transpose`) on the NPU; those segments are automatically partitioned onto `HostStage` (`onnx.reference.ReferenceEvaluator`).

## Public Interface / Exports

- [`n1x_dla.c`](n1x_dla.c) / [`n1x_dla.h`](n1x_dla.h) / [`n1x_dla.py`](n1x_dla.py) (`libn1x_dla.so`)
  - `n1x_dla_open`, `n1x_dla_info`, `n1x_dla_submit`, `n1x_dla_resubmit`, `n1x_dla_read`, `n1x_dla_stats`, `n1x_dla_control`, and Python `Device` wrapper.
- [`n1x_compile.cpp`](n1x_compile.cpp) / [`n1x_compiler.py`](n1x_compiler.py) (`libn1x_compiler.so`, `n1x-compile`)
  - `n1x_compiler_init`, `n1x_compile_json_graph`, `n1x_compiler_free_buffer`, and Python `compile_dag`, `onnx_to_dag`, `compile_onnx`.
- [`n1x_ort.cpp`](n1x_ort.cpp) / [`n1x_ort.py`](n1x_ort.py) (`libn1x_ort.so`, `n1x-ort-run`)
  - C ABI: `n1x_ort_module_create`, `n1x_ort_pack_fp32`, `n1x_ort_unpack_fp32`, `n1x_ort_module_run_fp32`, `n1x_ort_module_destroy`.
  - Python API: `N1xInferenceSession` (`InferenceSession`), `canonicalize_onnx_model`, `calibrate_onnx_model`, `partition_onnx_model`, `N1xOrtClient`.
- [`cudla_model.hpp`](cudla_model.hpp) / [`cudla_model.cpp`](cudla_model.cpp) / [`loadable.py`](loadable.py)
  - `n1x::Model::from_bytes(...)` and Python `loadable.Model` for `.nvda` parsing and DMA task image construction.
- [`cudla_backend.cpp`](cudla_backend.cpp) / [`cudla_broker.cpp`](cudla_broker.cpp) / [`cudla_broker.py`](cudla_broker.py)
  - Reconstructed `libnvcudla.so` backend for NVIDIA's Linux `libetsoc_cudla.so` frontend and the unprivileged UNIX-socket broker client/server.

## Dependencies & Consumers

- **Internal Dependencies**: [`include/`](../include/README.md) (`include/n1x_ort.h`, `include/n1x_compiler.h`, `include/uapi/n1x_dla_accel.h`).
- **External Dependencies**: `glibc`, `libstdc++`, `libcrypto` (OpenSSL SHA-256), Python `numpy` and `onnx`, and pinned vendor compiler binary `libnvdla_compiler.so` (`t254`) for offline/ahead-of-time compilation.
- **Primary Consumers**: [`demo/`](../demo/README.md) (`demo/superres.py`, `demo/vision.py`, `demo/ort_demo.py`) and [`scripts/`](../scripts/README.md) (`scripts/test_n1x_ort.py`, `scripts/test_n1x_compiler.py`, `scripts/qualify_n1x_ort.py`, `scripts/qualify_cudla.py`).

## Operational Contracts & Invariants

- **DLA Surface Alignment & Padding**: All `CHW16` (`FP16`, `line_stride = width * 32`) and `CHW32` (`INT8`, `line_stride = width * 32`) tensor surfaces are padded to 32-byte channel-group boundaries; elements in the padded tail (`c >= C`) are zero-filled before submission.
- **Caffe `ceil` Pool & `FullyConnected` Lowering**: Because `libnvdla_compiler.so` (`t254`) uses Caffe-style `ceil` output sizing on `MaxPool`/`AveragePool`, [`n1x_compile.cpp`](n1x_compile.cpp) lowers `FullyConnected`/`Gemm` as a `Convolution` with spatial `kernel = [in_h, in_w]` and `stride = [min(in_h, 8), min(in_w, 8)]`, computing the exact `1x1` inner product over `[0:in_h, 0:in_w]` even when an upstream pool adds a 1-pixel right/bottom Caffe border.
- **`REEXEC_IO` Fast Path**: Both [`n1x_ort.cpp`](n1x_ort.cpp) and [`cudla_backend.cpp`](cudla_backend.cpp) submit the full `.nvda` image once via `n1x_dla_submit` (`DRM_IOCTL_N1X_DLA_EXEC`) and reuse the retained kernel job handle via `n1x_dla_resubmit` (`DRM_IOCTL_N1X_DLA_REEXEC`) on all subsequent runs.
