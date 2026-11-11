# Architecture & Protocol Reference

## 1. Hardware Topology & ACPI Binding

The NVIDIA N1X (`t254`) NPU exposes two ACPI platform devices that are bound as an aggregate device using `<linux/component.h>` ([`driver/n1x_npu_boot_linux.c`](../driver/n1x_npu_boot_linux.c)):

| ACPI HID | `_HRV` | Role | SMMUv3 Stream ID | DMA Coherency |
| --- | --- | --- | --- | --- |
| `NVDA200A` (`NVDA2042`) | `0x34` | DLA HAL-1 MMIO aperture (`0x40000` B), HFRP mailbox, GSI `234` IRQ | `0x2b` | Coherent (`_CCA = 1`) |
| `NVDA8600` | `0x01` | `SROOT` secure firmware upload DMA requester (`180` B MMIO) | `0x01` | Noncoherent (`_CCA = 0`) |

The driver discovers both `_CRS` MMIO apertures, IORT SMMUv3 mappings, and the live VBIOS policy carveout dynamically without hardcoded DMI board/BIOS checks.

Each `NVDA8600` supplier owns a reference-counted boot context containing
its loader, DMA allocations, HFRP channel, queue, IRQ accounting and PM state.
The DLA component joins that context through an exact ACPI match. Matching
requires one SROOT sibling with `_HRV=1` under the same firmware parent;
missing or ambiguous associations fail closed. No global boot-state instance
or first-match supplier fallback remains. The global registry contains only
referenced contexts. The platform-specific VBIOS locator is still fixed to
this SoC; context isolation does not establish support for other hardware.

A stateless DLA → SROOT device link orders system PM. Device-bound DSP clients
add DSP → DLA ordering at probe and reserve the shared provider before DSP
memory publication. Legacy upload ABIs v1/v2 require a unique registered
context and reject ambiguity. Component unbind drains the DLA IRQ after
clearing its borrowed context pointer. A context reference acquired before
removal remains valid, but detached contexts cannot admit hardware work.
Published DMA keeps both its context and DLA-device references until verified
shutdown. Failed
shutdown remains terminal; a retained context cannot be rebound and retried.
This is not a claim of general reset/recovery or upstream readiness.
See [kernel driver architecture](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux/blob/main/docs/kernel-driver-architecture.md)
for subsystem boundaries and remaining upstream requirements.

### Typed DSP resume command

The additive kernel export `n1x_sroot_reinit_dsp_v1()` submits the native
SROOT REINIT command (`0x1a`, three words: image ID, zero, mode 1) for the
acknowledged image bound to a retention-aware client. It requires a genuine
provider PM generation and a sleeping or rollback client while the provider
is awake. Each generation permits one attempt; uncertain, rejected or malformed
replies poison the loader. The operation allocates/releases no DMA memory and
does not release client/module/PM leases. Existing v1–v4 operation tables retain
their layouts and behavior.

The pinned Windows `nvadsp.sys` (2.13.0.1, SHA256
`8b21e4652bb0ac178a2c470c8d6f982155e715c46112ac3b93de3ebfe00b13d6`)
marks reinitialization needed at D0 exit (`0x140007630`). Its next activation
calls `0x14004e000`: write the shared operating-DRAM base/size, submit the
four-byte image ID through IOCTL `0x150218`, and wait for firmware ready.
In pinned `nvsrootmbx.sys` (SHA256
`f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a`),
`NotifySrootReinitFw` at `0x140031760` expands that ID into a zero-extended
64-bit value followed by mode 1 and submits command `0x1a` with three payload
words. It checks transport completion and remote status.

This provider operation has offline fault/lifetime coverage. Complete DSP
resume is not qualified: its consumer must restore the startup descriptor,
require a fresh firmware-ready event, and rebuild any invalidated task graph
before resuming ALSA. A device-only suspend test restored the NPU/provider but
found changed DSP SRAM and correctly blocked the old retention-only consumer.
The new export is not a userspace reset/recovery interface.

## 2. VBIOS Policy & HFRP Bring-Up

### Live VBIOS Carveout (GB10/T264 display / `0x1182bc80`)
[`n1x_boot_live_board_policy()`](../driver/n1x_npu_boot_linux.c) reads the GB10/T264 display locator register (`0x1182bc80`), computes the 32-bit physical carveout base (`(((u64)locator << 12) + N1X_VBIOS_CARVEOUT_BASE) & 0xffffffffULL`, yielding `0xe5b81800` for locator `0xffe5b812`), and parses the BIT `0x6c` table ([`driver/n1x_dla_vbios.c`](../driver/n1x_dla_vbios.c)) to extract the board's clock/power/gating policy (`892.8 MHz` nominal, gating mask `0x70`).

### 8-Stage In-Kernel Bring-Up
When both endpoints bind, [`n1x_master_bind()`](../driver/n1x_npu_boot_linux.c) runs the 8-stage initialization sequence in C:

1. **`prepare_hfrp`**: Resumes runtime PM, performs HFRP handshake and version discovery (`v1.5`), sends the VBIOS policy table ([`driver/n1x_hfrp_setup.c`](../driver/n1x_hfrp_setup.c)), and powers on the DLA rails/clocks (HFRP command `400` / `0x190` in [`driver/n1x_hfrp.c`](../driver/n1x_hfrp.c)).
2. **`upload`**: Streams `nvidia/n1x/sroot-v0.bin` over the noncoherent `NVDA8600` `SROOT` DMA channel and verifies the firmware boot response.
3. **`ping`**: Sends HAL-1 method `1` (`boot_ping.dma >> 8`) to verify RISC-V firmware DMA readiness (`0x44444444`) and captures the owned interrupt route mask (`0xd2c`).
4. **`init_region5`**: Allocates and registers the 1 MiB Region 5 telemetry/trace ring buffer.
5. **`read_config` & `apply_config`**: Queries and applies the HAL-1 engine configuration snapshot.
6. **`apply_lpwr`**: Configures low-power gating thresholds.
7. **`bypass_task`**: Submits the initial bypass task to transition the firmware scheduler to ready state.
8. **`preempt` (`enable_preempt=true`)**: Sends method `0x31c` (`data = 1` $\rightarrow$ `0x1c02`) while idle to enable firmware checkpoint preemption (`0x183728`).

### Interrupt Handling (`enable_irq=true`)
[`n1x_boot_irq_handler()`](../driver/n1x_npu_boot_linux.c) handles GSI `234`: it checks pending bits (`0x08`) against `owned` (`0xd2c`) and `saved` (`0xd28`), masks active bits via `0xd24` (`saved & owned`), increments `boot_irq_count`, and wakes `boot_waitq`.

## 3. `/dev/accel/accel0` UAPI & DMA Lifetime

Defined in [`include/uapi/n1x_dla_accel.h`](../include/uapi/n1x_dla_accel.h) and implemented in [`driver/n1x_dla_accel.c`](../driver/n1x_dla_accel.c):

| Ioctl / Operation | Description |
| --- | --- |
| `DRM_IOCTL_N1X_DLA_INFO` | Returns ABI version (`2`), capability bitmask (`REEXEC_IO`, `CONTROLLED_STOP`, `STATISTICS`, `PREEMPT`), and active job/byte counters |
| `DRM_IOCTL_N1X_DLA_EXEC` | Validates address-list relocations, allocates coherent DMA memory, copies the task image, and executes (or registers as `RESUMABLE`) |
| `DRM_IOCTL_N1X_DLA_REEXEC` | Patches only caller-declared `writable_ranges` (input/output tensors) in-place and re-executes with zero new DMA allocations |
| `DRM_IOCTL_N1X_DLA_READ` | Copies output tensor or statistics slices from a caller-owned completed job |
| `DRM_IOCTL_N1X_DLA_STATS` | Validates v2 (`0x00000300`, version `2`) hardware layer profiling records ([`driver/n1x_dla_statistics.c`](../driver/n1x_dla_statistics.c)) |
| `DRM_IOCTL_N1X_DLA_CONTROL` | Step-wise `START`, `POLL`, `PREEMPT` (method `0x212` / `0x120b`), and `RESUME` (method `0x307`) |
| `mmap()` (`job_id << PAGE_SHIFT`) | Zero-copy read-only (`VM_READ`, `!VM_MAYWRITE`) userspace mapping of a completed job's DMA buffer via `dma_mmap_coherent()` |

**Lifetime & Fail-Closed Retention**:
- Firmware publication retains module/runtime-PM leases until verified shutdown.
  A DRM registration failure after startup keeps the components bound so devres
  cannot unmap live hardware. Checked shutdown remains available for reload.
- Completed jobs owned by an open `/dev/accel/accel0` file descriptor are freed automatically in [`postclose()`](../driver/n1x_dla_accel.c) when the FD closes (or in [`n1x_accel_vm_close()`](../driver/n1x_dla_accel.c) when the last `mmap` VMA unmaps).
- While `job->mmap_count > 0`, `REEXEC` returns `-EBUSY` so DMA never races with userspace reads.
- Any job that fails or times out—or any device instance where a hardware power-down attempt failed (`accel->stop_failed`)—retains its DMA pages until confirmed hardware shutdown or reboot.
- Multi-value diagnostic state (`status`, `hfrp_status`, `shutdown_status`) is exposed via `debugfs` under `/sys/kernel/debug/n1x-npu-bootstrap/`, while `/sys/bus/platform/devices/NVDA200A:00/` exposes only single-action write attributes (`shutdown`, `query_power_state`).

## 4. Native `t254` Compiler (`libn1x_compiler.so`, `n1x-compile`)

Implemented in [`runtime/n1x_compiler_shim.cpp`](../runtime/n1x_compiler_shim.cpp), [`runtime/n1x_compile.cpp`](../runtime/n1x_compile.cpp), and [`runtime/n1x_compiler.py`](../runtime/n1x_compiler.py):

- **ARM64 `Mtl3` Bridge (`-ffixed-x18`)**: Maps the `t254` backend (`Mtl3`) from `nvdla_compiler.dll` into Linux `libnvdla_compiler.so` in-process while preserving the Windows ARM64 TEB register (`x18`) and serializing concurrent compiler invocations (`g_compile_mutex`).
- **Precision, Profile & Quantization**: Supports `FP16` (`CHW16`) and `INT8` (`CHW32`, per-tensor and per-channel quantization scales, explicit `QuantizeLinear`/`DequantizeLinear`), configurable `IProfile` flags (`generate_stats` / `task-0-stats`, Winograd, weight compression, `sdp_pdp_on_fly`, `use_softmax_optz`, memory pool sizes), and free-list heap reuse across multi-branch DAGs.
- **Operator Lowering**: Lowers `Conv`, `ConvTranspose` (`Deconvolution`), `Gemm`/`FullyConnected`/`MatMul`, `Relu`/`Sigmoid`/`Tanh`/`Clip`/`LeakyRelu`/`Elu`/`Selu`/`Softsign`/`Softplus`/`Abs`, `MaxPool`/`AveragePool`/`GlobalAveragePool`, `Add`/`Sub`/`Mul`/`Div`/`Max`/`Min`, `Concat`, `BatchNormalization`, `LayerNormalization`/`InstanceNormalization`/`RMSNormalization`, `LRN`, `Scale`, `Slice`, `Resize`/`Upsample` (nearest & 2x2 bilinear), `DepthToSpace` (`PixelShuffle`), `Softmax`, `ReduceMean`/`ReduceSum`/`ReduceMax`/`ReduceMin`/`ReduceL1`/`ReduceL2`/`ReduceSumSquare`, `Unary` math ops (`Sin`, `Cos`, `Atan`, `Exp`, `Log`, `Reciprocal`, `Ceil`, `Floor`, `Round`, `Pow`, `Sqrt`, `Rsqrt`, `Erf`, `Neg`, `Sign`, `Tan`, `Sinh`, `Cosh`, `Asin`, `Acos`), `Pad`, `Tile`, `Gather`/`GatherElements`, `Select`/`Where`, `Reverse`, `LSTM`, and `ArgMax`/`ArgMin` into `.nvda` FlatBuffers loadables.

## 5. Heterogeneous ONNX Execution Provider (`libn1x_ort.so`, `N1xInferenceSession`)

Implemented in [`runtime/n1x_ort.cpp`](../runtime/n1x_ort.cpp) and [`runtime/n1x_ort.py`](../runtime/n1x_ort.py):

1. **Canonicalization**: Resolves `auto_pad` (`SAME_UPPER`/`SAME_LOWER`), folds constant subgraphs, fuses `Conv+Add` and `Flatten/Reshape+MatMul+Add` $\rightarrow$ `Gemm`, and rewrites opset-10 `Reshape->Transpose->Reshape` sub-pixel tails into `DepthToSpace`.
2. **Partitioning, Caching & Calibration**: Partitions arbitrary ONNX graphs into cycle-free topological `DLA` and `Host` stages, compiles each `DLA` stage into an `.nvda` loadable cached by SHA-256 under `~/.cache/n1x/ort/`, and calibrates both per-tensor (`tensor_ranges`) and per-channel (`channel_ranges` / `channel_dynamic_ranges`) `INT8` scales via `calibrate_onnx_model()`.
3. **Execution, `mmap` Readback & Profiling**: Packs and unpacks `NCHW` `float32` tensors into `CHW16` (`FP16`) or `CHW32` (`INT8`) surfaces with ARM64 NEON vectorization (`n1x_ort_pack_fp32` / `n1x_ort_unpack_fp32`), checks retained-job kernel budgets via `n1x_dla_info()`, executes via `EXEC` on initial run and `REEXEC_IO` on subsequent runs, reads outputs via zero-copy read-only `mmap()` (`n1x_dla_mmap()`) when available, and exposes `task-0-stats` profiling blobs (`n1x_ort_module_read_stats()`).

## 6. Linux cuDLA Backend (`libnvcudla.so`)

Implemented in [`runtime/cudla_backend.cpp`](../runtime/cudla_backend.cpp) and [`runtime/cudla_model.cpp`](../runtime/cudla_model.cpp):

- Implements the backend driver interface expected by NVIDIA's unmodified Linux `libcudla.so.1` (`13.4.92`), staging registered CUDA device buffers into page-aligned `.nvda` task images and submitting them to `/dev/accel/accel0`.
- Supports `CUDLA_TASK_STATISTICS` (`DRM_IOCTL_N1X_DLA_STATS`), `CUDLA_DEVICE_VERSION`, `cudlaSetTaskTimeoutInMs`, and compact `REEXEC_IO` staging on repeat submissions (allocating and staging only the dynamic input/output/statistics slices instead of the full `.nvda` weight image).

### Device-specific diagnostics

Each SROOT supplier has a diagnostic directory named after its platform device
below `/sys/kernel/debug/n1x-npu-bootstrap/`, with `status`, `hfrp_status` and
`shutdown_status`. Open files retain a device reference and resolve a live
context for each read. Removed devices return `ENODEV`. The three original
root-level files remain a compatibility view only when exactly one provider
is registered; multiple providers return `ENOTUNIQ`.
