# N1X Backend for NVIDIA cuDLA (`libnvcudla.so`)

[`runtime/cudla_backend.cpp`](../runtime/cudla_backend.cpp) and [`runtime/cudla_model.cpp`](../runtime/cudla_model.cpp) implement the backend driver interface loaded by NVIDIA's Linux `libcudla.so.1` (`13.4.92`), translating cuDLA module loads, CUDA memory registrations, and task submissions into `/dev/accel/accel0` ioctls.

## Build and Test

```sh
make cudla
make test-cudla
```

Produces:
- `runtime/cudla/libnvcudla.so`: Backend shared library loaded by `libcudla.so.1`.
- `runtime/cudla/cudla-smoke`: Direct CLI test linking `libcudla.so.1`.

## Supported cuDLA Features

- **Hybrid CUDA Mode**: Supports up to 16 device handles bound to the active CUDA context.
- **Loadable Deserialization**: Parses `.nvda` FlatBuffers loadables, validates address-list entries and `task-0-stats` v2 profiling descriptors, and registers input/output tensor descriptors sorted by bind ID.
- **Synchronous Stream Staging & `REEXEC_IO`**: Enqueues async device-to-host input copies on the caller's `CUstream`, synchronizes before `DRM_IOCTL_N1X_DLA_EXEC` (initial run) or `DRM_IOCTL_N1X_DLA_REEXEC` (subsequent runs), reads outputs via `DRM_IOCTL_N1X_DLA_READ`, and copies results back to CUDA memory on the caller's stream.
- **Profiling & Attributes**: Supports `CUDLA_NUM_OUTPUT_TASK_STATISTICS`, `CUDLA_OUTPUT_TASK_STATISTICS_DESCRIPTORS`, `CUDLA_TASK_STATISTICS` (`DRM_IOCTL_N1X_DLA_STATS`), `CUDLA_DEVICE_VERSION` (`0x0200`), `cudlaSetTaskTimeoutInMs` (`1..1000000` ms), `CUDLA_SUBMIT_NOOP`, and `CUDLA_SUBMIT_SKIP_LOCK_ACQUIRE`.

## Hardware Qualification

Run the 8-job baseline or `--reuse` (`REEXEC_IO`) physical qualification suite:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_cudla.py" \
  --reuse --output-dir "$PWD/results/NEW-cudla-reuse"
```
