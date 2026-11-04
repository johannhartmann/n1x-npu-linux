# Architecture & Protocol Reference

## 1. Hardware Topology & ACPI Binding

The NVIDIA N1X (`t254`) NPU exposes two ACPI platform devices that are bound as an aggregate device using `<linux/component.h>` ([`driver/n1x_npu_boot_linux.c`](../driver/n1x_npu_boot_linux.c)):

| ACPI HID | `_HRV` | Role | SMMUv3 Stream ID | DMA Coherency |
| --- | --- | --- | --- | --- |
| `NVDA200A` (`NVDA2042`) | `0x34` | DLA HAL-1 MMIO aperture (`0x40000` B), HFRP mailbox, GSI `234` IRQ | `0x2b` | Coherent (`_CCA = 1`) |
| `NVDA8600` | `0x01` | `SROOT` secure firmware upload DMA requester (`180` B MMIO) | `0x01` | Noncoherent (`_CCA = 0`) |

When `strict_platform=0` (default), the driver discovers both `_CRS` MMIO apertures, IORT SMMUv3 mappings, and the live VBIOS policy carveout dynamically without hardcoded DMI board/BIOS checks.

## 2. VBIOS Policy, HFRP Bring-Up & Frequency Scaling

### Live VBIOS Carveout (`0x1182bc80`)
[`n1x_boot_live_board_policy()`](../driver/n1x_npu_boot_linux.c) reads the display VBIOS locator register at `0x1182bc80` (`N1X_DISP_VBIOS_LOCATOR_PHYS`), computes the physical carveout base (`((u64)(locator >> 12) << 16) + 0x40000`), and parses the BIT `0x6c` table ([`driver/n1x_dla_vbios.c`](../driver/n1x_dla_vbios.c)) to extract the board's clock/power/gating policy (`892.8 MHz` nominal, up to `1500 MHz` boost, gating mask `0x70`).

### 8-Stage In-Kernel Bring-Up (`autostart=1`)
When both endpoints bind, `n1x_boot_master_bind()` runs the 8-stage initialization sequence in C:

1. **`prepare_hfrp`**: Resumes runtime PM, performs HFRP handshake and version discovery (`v1.5`), sends the VBIOS policy table, and powers on the DLA rails/clocks (`POWER_CONTROL` `0x400`).
2. **`upload`**: Streams `nvidia/n1x/p16-sroot-v0.bin` over the noncoherent `NVDA8600` `SROOT` DMA channel and verifies the firmware boot response.
3. **`ping`**: Executes a mailbox ping (`0x101`) to verify RISC-V firmware readiness and captures the owned interrupt route mask (`0xd2c`).
4. **`init_region5`**: Allocates and registers the 1 MiB Region 5 telemetry/trace ring buffer.
5. **`read_config` & `apply_config`**: Queries and applies the HAL-1 engine configuration snapshot.
6. **`apply_lpwr`**: Configures low-power gating thresholds.
7. **`bypass_task`**: Submits the initial bypass task to transition the firmware scheduler to ready state.
8. **`preempt` (`enable_preempt=true`)**: Sends method `0x31c` (`data = 1` $\rightarrow$ `0x1c02`) while idle to enable firmware checkpoint preemption (`0x183728`).

### Dynamic Frequency Scaling (`npu_freq_khz`)
`/sys/bus/platform/devices/NVDA200A:00/npu_freq_khz` reads or updates the active HFRP clock frequency between `892800` kHz (`892.8 MHz`) and `1500000` kHz (`1500 MHz`).

### Interrupt Handling (`enable_irq=true`)
[`n1x_boot_irq_handler()`](../driver/n1x_npu_boot_linux.c) implements the `nvlddmkm.sys` (`0xd49800`) top-half contract: on GSI `234`, it checks pending bits (`0x08`) against `owned` (`0xd2c`) and `saved` (`0xd28`), masks active bits via `0xd24` (`saved & owned`), increments `boot_irq_count`, and wakes `boot_waitq`.

## 3. `/dev/accel/accel0` UAPI & DMA Lifetime

Defined in [`include/uapi/n1x_dla_accel.h`](../include/uapi/n1x_dla_accel.h) and implemented in [`driver/n1x_dla_accel.c`](../driver/n1x_dla_accel.c):

| Ioctl / Operation | Description |
| --- | --- |
| `DRM_IOCTL_N1X_DLA_INFO` | Returns ABI version (`2`), capability bitmask (`REEXEC_IO`, `CONTROLLED_STOP`, `STATISTICS`, `PREEMPT`, `MMAP`), and active job/byte counters |
| `DRM_IOCTL_N1X_DLA_EXEC` | Validates address-list relocations, allocates coherent DMA memory, copies the task image, and executes (or registers as `RESUMABLE`) |
| `DRM_IOCTL_N1X_DLA_REEXEC` | Patches only caller-declared `writable_ranges` (input/output tensors) in-place and re-executes with zero new DMA allocations |
| `DRM_IOCTL_N1X_DLA_READ` | Copies output tensor or statistics slices from a caller-owned completed job |
| `DRM_IOCTL_N1X_DLA_STATS` | Validates v2 (`0x00000300`, version `2`) hardware layer profiling records ([`driver/n1x_dla_statistics.c`](../driver/n1x_dla_statistics.c)) |
| `DRM_IOCTL_N1X_DLA_CONTROL` | Step-wise `START`, `POLL`, `PREEMPT` (method `0x212` / `0x120b`), and `RESUME` (method `0x307`) |
| `mmap()` (`job_id << PAGE_SHIFT`) | Zero-copy read-only (`VM_READ`, `!VM_MAYWRITE`) userspace mapping of a completed job's DMA buffer via `dma_mmap_coherent()` |

**Lifetime & Fail-Closed Retention**:
- Completed jobs owned by an open `/dev/accel/accel0` file descriptor are freed automatically in [`n1x_accel_postclose()`](../driver/n1x_dla_accel.c) when the FD closes (or in [`n1x_accel_vm_close()`](../driver/n1x_dla_accel.c) when the last `mmap` VMA unmaps).
- While `job->mmap_count > 0`, `REEXEC` returns `-EBUSY` so DMA never races with userspace reads.
- Any job that fails or times out—or any device instance whose HFRP power-down (`43 00` / state `07`) fails (`accel->stop_failed`)—retains its DMA pages until confirmed hardware shutdown or reboot.

## 4. Native `t254` Compiler (`libn1x_compiler.so`, `n1x-compile`)

Implemented in [`runtime/n1x_compiler_shim.cpp`](../runtime/n1x_compiler_shim.cpp), [`runtime/n1x_compile.cpp`](../runtime/n1x_compile.cpp), and [`runtime/n1x_compiler.py`](../runtime/n1x_compiler.py):

- **ARM64 `Mtl3` Bridge (`-ffixed-x18`)**: Maps the `t254` backend (`Mtl3`) from `nvdla_compiler.dll` into Linux `libnvdla_compiler.so` in-process while preserving the Windows ARM64 TEB register (`x18`).
- **Precision & Quantization**: Supports `FP16` (`CHW16`) and `INT8` (`CHW32`, per-tensor and per-channel quantization scales) with free-list heap reuse across multi-branch DAGs.
- **Operator Lowering**: Lowers `Conv`, `Gemm`/`FullyConnected`, `Relu`, `Clip`, `MaxPool`, `AveragePool`, `GlobalAveragePool`, `Add`, `Sub`, `Mul`, `Div` (constant divisor $\rightarrow$ reciprocal `Scale`), `Concat`, `BatchNormalization`, `Scale`, `Sigmoid`, and `Tanh` directly into `.nvda` FlatBuffers loadables.

## 5. Heterogeneous ONNX Execution Provider (`libn1x_ort.so`, `N1xInferenceSession`)

Implemented in [`runtime/n1x_ort.cpp`](../runtime/n1x_ort.cpp) and [`runtime/n1x_ort.py`](../runtime/n1x_ort.py):

1. **Canonicalization**: Resolves `auto_pad` (`SAME_UPPER`/`SAME_LOWER`), folds constant subgraphs, fuses `Conv+Add` and `Flatten/Reshape+MatMul+Add` $\rightarrow$ `Gemm`, and rewrites opset-10 `Reshape->Transpose->Reshape` sub-pixel tails into `DepthToSpace`.
2. **Partitioning & Caching**: Partitions arbitrary ONNX graphs into cycle-free topological `DLAStage` and `HostStage` segments, compiles each `DLAStage` into an `.nvda` loadable cached by SHA-256 under `~/.cache/n1x_ort/`, and auto-calibrates `INT8` tensor/channel scales when requested.
3. **Execution**: Packs `NCHW` `float32` tensors into `CHW16` (`FP16`) or `CHW32` (`INT8`) surfaces in C++17 (`n1x_ort_pack_fp32` / `n1x_ort_unpack_fp32`) and executes via `EXEC` on initial run and `REEXEC_IO` on subsequent runs.

## 6. Linux cuDLA Backend (`libnvcudla.so`) & IPC Broker

Implemented in [`runtime/cudla_backend.cpp`](../runtime/cudla_backend.cpp), [`runtime/cudla_model.cpp`](../runtime/cudla_model.cpp), and [`runtime/cudla_broker.cpp`](../runtime/cudla_broker.cpp):

- Implements the backend driver interface expected by NVIDIA's unmodified Linux `libcudla.so.1` (`13.4.92`), staging registered CUDA device buffers into page-aligned `.nvda` task images and submitting them to `/dev/accel/accel0`.
- Supports `CUDLA_TASK_STATISTICS` (`DRM_IOCTL_N1X_DLA_STATS`), `CUDLA_DEVICE_VERSION`, `cudlaSetTaskTimeoutInMs`, and `REEXEC_IO` buffer reuse.
