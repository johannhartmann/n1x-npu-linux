# Security Policy

## Access Control & DMA Isolation

- **Device Permissions (`require_rawio=0`)**: By default, `/dev/accel/accel0` uses standard VFS permissions (`0660 root:render`, `TAG+="uaccess"` via [`packaging/udev/99-n1x-npu.rules`](packaging/udev/99-n1x-npu.rules)). Passing `require_rawio=1` restricts device open and `ioctl` operations to `CAP_SYS_RAWIO`.
- **Trusted Loadables**: Kernel metadata and address-list checks validate buffer bounds and restrict `DRM_IOCTL_N1X_DLA_REEXEC` patches to caller-declared `writable_ranges`, but do not sandbox embedded DLA RISC-V/engine instructions. Only load trusted `.nvda` and ONNX models.
- **DMA & `mmap()` Lifetime**: Completed jobs are freed automatically in `postclose()` when the owning file descriptor closes and all read-only `mmap()` VMAs close (`n1x_accel_vm_close()`). While a job is mapped (`job->mmap_count > 0`), `REEXEC` is rejected with `-EBUSY`. Any job that fails or times out—or any device instance whose HFRP power-down fails (`accel->stop_failed`)—retains its DMA pages until confirmed hardware shutdown or reboot.

## Reporting a Vulnerability

Report security issues via GitHub's [private vulnerability reporting](https://github.com/johannhartmann/n1x-npu-linux/security/advisories/new) with the commit hash, kernel release, and reproduction steps.
