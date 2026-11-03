# Linux N1X NPU Kernel Driver (`driver/`)

## Purpose

Implements the Linux kernel module `n1x_npu_bootstrap.ko` for the NVIDIA N1X (`NVDA200A` NPU endpoint and `NVDA8600` `SROOT` DMA requester), managing Host-to-Firmware Request Protocol (HFRP) power/clock state, RISC-V firmware upload, noncoherent `SROOT` DMA memory management, dynamic NPU frequency scaling (`892.8 MHz`–`1500 MHz`), and the `/dev/accel/accel0` DRM accelerator character device.

## Key Concepts

- **Dual ACPI Endpoint Binding (`n1x_npu_boot_linux.c`, `n1x_sroot.c`)**: Binds both the MMIO/interrupt NPU device (`NVDA200A`) and the noncoherent DMA requester (`NVDA8600` `SROOT`), exports `MODULE_DEVICE_TABLE(acpi, n1x_boot_ids)` for standard `udev`/`kmod` autoloading, and registers `/dev/accel/accel0` only after both endpoints are bound and initialized.
- **In-Kernel Automatic Bring-Up & Unload Teardown (`n1x_npu_boot_linux.c`)**: By default (`autostart=1`), executes the full 8-stage bring-up sequence (`prepare_hfrp` $\rightarrow$ `upload` $\rightarrow$ `ping` $\rightarrow$ `init_region5` $\rightarrow$ `read_config` $\rightarrow$ `apply_config` $\rightarrow$ `apply_lpwr` $\rightarrow$ `bypass_task`) automatically in C when the second endpoint probes, and performs confirmed firmware power-down (`n1x_boot_stop_device`) automatically in `n1x_boot_remove()` on `modprobe -r` / `rmmod`.
- **Per-FD Job Lifetime & `REEXEC_IO` Reuse (`n1x_dla_accel.c`, `n1x_dla_job.c`)**: Snapshots verified `.nvda` task graphs into immutable kernel DMA allocations on `DRM_IOCTL_N1X_DLA_EXEC`, patches only declared writable input/output surface ranges in-place on `DRM_IOCTL_N1X_DLA_REEXEC` (`0` new DMA allocations), and automatically frees completed jobs owned by a file descriptor when it closes (`postclose`), while retaining any failed/timed-out DMA buffers until confirmed hardware shutdown.
- **Dynamic Hardware & VBIOS Policy Discovery (`n1x_npu_boot_linux.c`, `n1x_dla_vbios.c`)**: By default (`strict_platform=0`), dynamically discovers the NPU (`NVDA200A`) and `SROOT` (`NVDA8600`) `_CRS` MMIO apertures, IORT SMMUv3 stream mappings, and live VBIOS BIT `0x6c` clock/power/gating policy tables from the hardware carveout locator (`0x1182bc80`), and exposes `/sys/bus/platform/devices/NVDA200A:00/npu_freq_khz` (`892800` kHz default, up to `1500000` kHz boost).

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

- **Standard `udev` / `render` Device Access (`require_rawio=0`)**: By default (`require_rawio=0`), `/dev/accel/accel0` uses standard VFS permissions (`0660`, `GROUP="render"`, `TAG+="uaccess"` via [`99-n1x-npu.rules`](../packaging/udev/99-n1x-npu.rules)), allowing local desktop and `render`-group users to run NPU workloads without root privileges (strict `CAP_SYS_RAWIO` enforcement remains available via `require_rawio=1`).
- **Writable Range Isolation**: `DRM_IOCTL_N1X_DLA_REEXEC` rejects any patch that falls outside the `writable_ranges` declared when the job was originally created by `DRM_IOCTL_N1X_DLA_EXEC`, protecting firmware command descriptors and weights from modification.
- **Per-FD Cleanup & Fail-Closed Error Retention**: Completed jobs owned by an open `/dev/accel/accel0` file descriptor are freed automatically in `postclose()` when the descriptor closes; any job that fails or times out remains pinned in DMA memory until confirmed hardware shutdown (`43 00` / state `07`).
