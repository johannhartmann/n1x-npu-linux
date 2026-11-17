# Documentation

- [Architecture & Protocol Reference](architecture.md): ACPI endpoints, VBIOS policy carveout, HFRP bring-up, `/dev/accel/accel0` UAPI, native `t254` compiler, and ONNX Execution Provider internals.
- [Setup and Reproduction](setup.md): Dependencies, pinned vendor inputs, firmware extraction, model compilation, and module build.
- [Support Status](status.md): Qualified hardware/software configuration and benchmark summary.
- [Robustness and Startup Diagnostics](robustness.md): Failure stages, retained handshake evidence, Surface issue #30, Windows reference and hardware recovery boundaries.
- [Offline Vendor-Code Replay](vendor-replay.md): Pinned Windows initializer and RV32 firmware-client execution, observed error propagation, and explicit emulator boundaries.
- [Controlled Shutdown and Reload](shutdown-reload.md): HFRP power-down contract, DMA/VMA lifetime, and reload qualification.
- [Shared SROOT DSP Clients](sroot-dsp-lifetime.md): Image-staging lifetime, retained provider leases and the DSP shutdown guard.
- [cuDLA Backend](cudla-backend.md): `libnvcudla.so` implementation and hardware qualification.
- [Arch Package & Kernel Updates](updates.md): Building and installing `n1x-npu-driver`.
- [Demos](../demo/README.md): Super-resolution (`./demo-superres`), [denoising & classification](../demo/vision.md) (`./demo-denoise`, `./demo-classify`), and [ONNX Execution Provider](../demo/README.md#heterogeneous-onnx-execution-provider-demo-demo-onnx) (`./demo-onnx`).
- [Scripts & Test Matrix](../scripts/README.md): Operational scripts and `Makefile` test targets.
