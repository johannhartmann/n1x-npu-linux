# Current support and qualification status

Updated 2026-11-01. This is the current summary; dated research documents retain
their earlier observations and failed experiments. Source-code implementation,
offline tests and physical execution are separate evidence levels.

## Tested hardware and software

- ASUS ProArt P16 H7607BA, BIOS `H7607BA.300`, NVIDIA RTX Spark / N1X.
- NPU ACPI `NVDA200A`, compatible ID `NVDA2042`, revision `0x34`, DLA HAL 1.
- SROOT ACPI `NVDA8600`, revision 1; its own noncoherent DMA requester.
- Kernel `7.2.8-23-rtx-spark-npu` and matching headers.
- Original NVIDIA Linux cuDLA 13.4.92 frontend, CUDA staging, reconstructed backend.
- Pinned Windows ARM64 driver 617.42 and TensorRT 11.4 compiler inputs.

Exact hashes and acquisition are in [setup](setup.md) and the qualification receipts.
The driver rejects other board/BIOS/kernel combinations. There is no claim of
universal N1X support or official NVIDIA Linux DLA support for this laptop.

## Physical results

The [eight-job cuDLA receipt](evidence/cudla-hardware-2026-10-28.json) covers:

| Graph | Inputs | Result |
| --- | --- | --- |
| FP16 ReLU | 2 different tensors | Exact independent reference matches |
| FP16 1x1 Conv with channel bias | 3 different tensors | Exact independent reference matches |
| FP16 Conv → ReLU | 3 different tensors | Exact independent reference matches |

All eight CUDA inputs remained unchanged. Firmware startup, DMA ping,
configuration and bypass passed first. The tested module retained 22,953,984
bytes for those eight jobs. That receipt describes the older retention-only
driver, not physical validation of newer reuse or cleanup code.

The trained four-convolution super-resolution graph also produced a correct
physical 64x64 input window. Its nine output planes had maximum absolute error
`0.0009882450` and RMSE `0.0000607435` against the independent FP32 ONNX reference.
See [window receipt](evidence/superres-tile-2026-10-28.json). A prior compiler artifact
produced incorrect numbers; correcting packed-constant replay fixed the window.
No full image or sustained throughput is established by this result.

## Current physical lifecycle and image results

Module `1ED6855E6645BEEF8DA3534` passed two complete healthy
stop/unload/reload cycles in boot `dfa2cb44-dcfd-424a-a884-71a107503e69`.
At each of three starts, the firmware/bypass/ReLU sequence passed, followed by
eight independent-process cuDLA jobs and eight same-module changing-input jobs.
All **48 outputs** exactly matched independent references. The reuse groups
performed **15 REEXECs** and consumed one allocation per graph.

Both stops returned power reply `43 00` and independent state `07`; every
retained accel allocation was released. EXEC after stop returned ENODEV.
An open client prevented ordinary unload; closing it allowed normal removal
of the module, accel node and both endpoint bindings. Fresh load, firmware
startup and inference succeeded each time, without a reboot between cycles.

The complete image demo then passed on the final running instance:

| Input → output | DLA windows | Pipeline seconds | Maximum absolute error | RMSE |
| --- | --- | --- | --- | --- |
| 512x512 → 1536x1536 | 100 | 0.856 | 0.00158405 | 0.000162065 |
| 1024x1024 → 3072x3072 | 361 | 2.181 | 0.00153911 | 0.000158844 |

Each image added exactly one 4,550,656-byte retained allocation. Learned
inference ran on the physical DLA; whole-image FP32 ONNX execution independently
validated the outputs. Timings are one staged pipeline run per size, not a
sustained benchmark. The 1024 input was a bicubic enlargement of the pinned
512 NASA sample to validate the larger grid, not a native-resolution quality
benchmark. The existing `performance` profile was recorded and not changed.

Exact hashes, stop evidence, allocation counters and measured results are in
the [current hardware receipt](evidence/npu-hardware-2026-10-28.json).
The [first failed attempt](evidence/reload-attempt-2026-10-28.json) and
[offline review](evidence/lifecycle-review-2026-10-28.json) remain historical evidence.
Their host-mask assertion and state-byte issues are corrected in this tested
build. No failure recovery or long-term reliability claim follows from two
healthy reload cycles.

The fresh-checkout compiler pipeline reconstructs the three fixture models and
the super-resolution loadable with their expected hashes, without loading a
driver or accessing a device. See [setup](setup.md) and the
[offline reproduction receipt](evidence/offline-reproduction-2026-10-28.json).

## Denoising and classification

The [additional vision examples](../demo/vision.md) passed physical execution
and independent FP32 ONNX reference checks on the same audited module/kernel:

| Example | Physical result |
| --- | --- |
| DnCNN-25 denoising | Complete 512x512 grayscale restoration, 64 windows / 192 NPU submissions per image; PSNR 20.73 -> 31.83 dB, SSIM 0.328 -> 0.847 with reproducible synthetic Gaussian noise |
| ResNet-18 classification | 224x224 center crop; all 20 convolutions on NPU in 27 submissions; dog example top-1 Samoyed, 88.4% model score, matching the CPU reference |
| Reuse across images | Two-image batches passed for each application; second images added zero retained allocations |

Across the six successful single-image/batch cases, 657 application submissions
passed. Denoising max absolute error was 0.00108639, RMSE 0.000175177. ResNet
logit max absolute error was <= 0.014134, RMSE <= 0.003637, with matching top-1
for both inputs. ResNet's ambiguous astronaut image is a numerical check, not
a claim of correct semantic classification.

Denoising uses three partitions and 25,817,088 retained bytes; classification
uses twelve and 65,777,664 bytes. Several images can share loaded partitions in
one invocation. Separate invocations allocate again and may require the healthy
manual stop/start sequence to release backing. The measured batch lifecycle
passed three healthy stops, all retained counters returned to zero, and fresh
startup succeeded. The device was left running without application allocations.
No new kernel faults were found and the selected power profile was unchanged.

The classifier uses CPU preprocessing, im2col layout gathering, pooling,
residual operations and its fully connected head. The denoiser uses CPU tiling,
grayscale conversion and noise subtraction. Neither is a full NPU-only pipeline
or evidence of a speed/energy advantage. Exact results, timings and their scope
are in the [vision receipt](evidence/vision-hardware-2026-10-29.json).

## Native `t254` compiler, `INT8` quantization and Heterogeneous ONNX Execution Provider

The repository now includes:

- **In-process `t254` Ahead-of-Time Compiler** ([`runtime/n1x_compile.cpp`](../runtime/n1x_compile.cpp), `libn1x_compiler.so`, `n1x-compile`, [`runtime/n1x_compiler.py`](../runtime/n1x_compiler.py)): Compiles JSON DAGs and ONNX subgraphs (`Conv`, `Gemm`/`FullyConnected`, `Relu`, `Clip`, `MaxPool`, `AveragePool`, `GlobalAveragePool`, `Add`, `Sub`, `Mul`, `Concat`, `BatchNormalization`, `Scale`, `Sigmoid`, `Tanh`) directly into `.nvda` FlatBuffers loadables in `FP16` and `INT8` (per-tensor and per-channel quantization).
- **C-Backed Heterogeneous ONNX Execution Provider** ([`runtime/n1x_ort.cpp`](../runtime/n1x_ort.cpp), `libn1x_ort.so`, `n1x-ort-run`, [`runtime/n1x_ort.py`](../runtime/n1x_ort.py), [design document](designs/n1x-onnx-execution-provider.md)):
  - Vectorized C++17 `FP32` $\leftrightarrow$ `CHW16` (`FP16`) / `CHW32` (`INT8`) surface packing and unpacking.
  - Canonical ONNX graph rewrites (`auto_pad` resolution, `Conv+Add` fusion, `Flatten/Reshape+MatMul+Add` $\rightarrow$ `Gemm`, opset-10 `Reshape->Transpose->Reshape` $\rightarrow$ `DepthToSpace`).
  - Cycle-free topological `DLAStage` + `HostStage` graph partitioning with SHA-256 `.nvda` disk caching and automatic multi-sample `INT8` calibration.
  - Physical qualification on `/dev/accel/accel0` ([hardware receipt](evidence/n1x-ort-hardware-2026-11-01.json)):
    - `mnist-12.onnx` (1-stage full-graph DLA `.nvda`, `2,975,356` B): `FP16` max abs error `0.005589`, RMSE `0.002614` (`0.833 ms` initial `EXEC`, `0.332 ms` `REEXEC_IO`); auto-calibrated `INT8` cosine similarity `0.999853` (`top-1` exact match).
    - `emotion-ferplus-8.onnx` (1-stage full-graph 37-node DLA `.nvda`, `21,210,412` B `FP16` / `12,465,324` B `INT8`): `FP16` max abs error `0.001340`, RMSE `0.000811` (`4.146 ms` initial `EXEC`, `2.077 ms` `REEXEC_IO`); auto-calibrated `INT8` cosine similarity `0.999930` (`6/6` expression gallery `top-1` matches).
    - `super-resolution-10.onnx` (2-stage heterogeneous `DLAStage` 4-Conv backbone + `HostStage` `DepthToSpace`): `FP16` max abs error `0.001510`, RMSE `0.000244` across `[1, 1, 192, 192]` (`1.357 ms` initial `EXEC`, `1.037 ms` `REEXEC_IO`).
    - 3-stage `DLA -> Host -> DLA` pipeline: `FP16` max abs error `0.000137`, auto-calibrated `INT8` MAE `0.000538`.
- **Dynamic NPU Frequency Scaling** ([`driver/n1x_dla_vbios.c`](../driver/n1x_dla_vbios.c)): Exposes `/sys/bus/platform/devices/NVDA200A:00/npu_freq_khz` (`892.8 MHz` default up to `1500 MHz` boost, reducing physical 64-tile super-resolution time from `224.2 ms` to `97.1 ms`, `2.31x` speedup).

## Remaining limits

Uncertain DMA/firmware errors remain fail-closed and require reboot. Automatic
recovery, suspend/resume, asynchronous scheduling, CUDA Graphs, zero-copy and
arbitrary TensorRT engine deserialization are unsupported. Non-DLA ONNX operators
(`DepthToSpace`, `ReduceMean`, `Resize`, `Softmax`, dynamic `Reshape`/`Transpose`)
execute on the host CPU via `N1xInferenceSession`'s heterogeneous partitioner.
The two successful healthy reload cycles do not validate fault recovery paths.

## Announcement scope

The verified scope is experimental Linux N1X/DLA support on this P16, with
cuDLA inference, native `t254` `FP16`/`INT8` compilation, heterogeneous ONNX
Execution Provider partitioning (`N1xInferenceSession`), bounded healthy
shutdown/reload qualification, complete super-resolution and denoising images,
and ResNet-18 classification. Vendor inputs and generated loadables are acquired
locally. The [setup](setup.md) and [demo](../demo/README.md) explain reproduction.
