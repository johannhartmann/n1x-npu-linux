# Linux N1X NPU Kernel Driver (`driver/`)

## Purpose

Implements the Linux kernel module `n1x_npu_bootstrap.ko` for the NVIDIA N1X (`NVDA200A` NPU endpoint and `NVDA8600` `SROOT` DMA requester), managing Host-to-Firmware Request Protocol (HFRP) power/clock state, RISC-V firmware upload, noncoherent `SROOT` DMA memory management, dynamic NPU frequency scaling (`892.8 MHz`–`1500 MHz`), and the `/dev/accel/accel0` DRM accelerator character device.

## Key Concepts

- **Dual ACPI Endpoint Binding (`n1x_npu_linux.c`, `n1x_sroot.c`)**: Binds both the MMIO/interrupt NPU device (`NVDA200A`) and the noncoherent DMA requester (`NVDA8600` `SROOT`), ensuring all device-visible task and tensor buffers are allocated and synced in `SROOT`'s IOMMU domain.
- **Staged Fail-Closed Startup & Shutdown (`n1x_hfrp_setup.h`, `n1x_dla_task.c`)**: Executes explicit, bounded bring-up stages (`prepare_hfrp` $\rightarrow$ `upload` $\rightarrow$ `ping` $\rightarrow$ `init_region5` $\rightarrow$ `read_config` $\rightarrow$ `apply_config` $\rightarrow$ `apply_lpwr` $\rightarrow$ `bypass_task`) and confirmed firmware power-down (`shutdown` $\rightarrow$ reply `43 00`, state `07`) before releasing retained DMA allocations.
- **Retained-Job Execution & `REEXEC_IO` Reuse (`n1x_dla_accel.c`, `n1x_dla_job.c`)**: Snapshots verified `.nvda` task graphs into immutable kernel DMA allocations on `DRM_IOCTL_N1X_DLA_EXEC`, and patches only declared writable input/output surface ranges in-place on `DRM_IOCTL_N1X_DLA_REEXEC` (`0` new DMA allocations).
- **Dynamic Frequency Scaling (`n1x_dla_vbios.c`)**: Parses the VBIOS clock/voltage descriptors and exposes `/sys/bus/platform/devices/NVDA200A:00/npu_freq_khz` (`892800` kHz default, up to `1500000` kHz boost).

## Boundaries & Non-Responsibilities

- **Does Not Do**: Does not parse FlatBuffers `.nvda` containers or compile neural network graphs inside the kernel; `.nvda` parsing, address-list materialization, and surface packing occur in userspace ([`runtime/`](../runtime/README.md)).
- **Does Not Do**: Never performs unconfirmed DMA freeing or automatic hardware reset on uncertain firmware timeouts (fails closed until controlled shutdown or reboot).

## Public Interface / Exports

- [`n1x_npu_linux.c`](n1x_npu_linux.c)
  - Platform driver `n1x_npu_bootstrap` and sysfs control attributes under `/sys/bus/platform/devices/NVDA200A:00/` (`prepare_hfrp`, `upload`, `ping`, `init_region5`, `read_config`, `apply_config`, `apply_lpwr`, `bypass_task`, `shutdown`, `npu_freq_khz`, `status`, `hfrp_status`, `shutdown_status`).
- [`n1x_dla_accel.c`](n1x_dla_accel.c)
  - `/dev/accel/accel0` DRM accelerator `ioctl` handlers: `info_ioctl`, `exec_ioctl`, `read_ioctl`, `reexec_ioctl`, `stats_ioctl`, `control_ioctl`.
- [`n1x_dla_vbios.c`](n1x_dla_vbios.c)
  - `n1x_dla_vbios_parse(...)` and dynamic HFRP frequency transition helpers (`892.8 MHz`–`1500 MHz`).
- [`n1x_dla_job.c`](n1x_dla_job.c), [`n1x_dla_job_control.c`](n1x_dla_job_control.c), [`n1x_dla_preempt.c`](n1x_dla_preempt.c)
  - Address-list relocation verification, writable-range enforcement, and task preemption/resume state machines.

## Dependencies & Consumers

- **Internal Dependencies**: [`include/`](../include/README.md) (`include/uapi/n1x_dla_accel.h`, `include/n1x_*.h`).
- **External Dependencies**: Linux kernel `7.2.8-23-rtx-spark-npu` (`drm_accel`, `dma-mapping`, `acpi`, `firmware`).
- **Primary Consumers**: [`runtime/n1x_dla.c`](../runtime/n1x_dla.c) (`libn1x_dla.so`), [`scripts/npu_autostart.py`](../scripts/npu_autostart.py), and [`scripts/qualify_p16_reload.py`](../scripts/qualify_p16_reload.py).

## Operational Contracts & Invariants

- **`CAP_SYS_RAWIO` Gate**: Opening `/dev/accel/accel0` and issuing any `DRM_IOCTL_N1X_DLA_*` command requires `CAP_SYS_RAWIO`. Unprivileged desktop applications must either route through the root-owned broker ([`runtime/cudla_broker.cpp`](../runtime/cudla_broker.cpp)) or run via an authorized launcher.
- **Writable Range Isolation**: `DRM_IOCTL_N1X_DLA_REEXEC` rejects any patch that falls outside the `writable_ranges` declared when the job was originally created by `DRM_IOCTL_N1X_DLA_EXEC`, protecting firmware command descriptors and weights from modification.
- **Retained Allocation Lifetime**: Retained job DMA buffers remain pinned while the NPU is active and are freed only after confirmed firmware shutdown (`43 00` / state `07`) during controlled `stop`.
