# Scripts and Test Matrix

## Operational Scripts

| Script | Description |
| --- | --- |
| `check_repo.py` | Validate Markdown links/anchors, Python syntax, JSON files, and demo asset hashes |
| `prepare_inputs.py`, `prepare_vision.py` | Verify (`default`), download (`--fetch`), or extract (`--extract-firmware`) pinned vendor inputs and vision checkpoints |
| `build_arch_module_package.py` | Build the `n1x-npu-driver` Arch package (`n1x_npu_bootstrap.ko`, `libn1x_{dla,compiler,ort}.so`, `n1x-{compile,ort-run}`, `n1x-npu.service`, `99-n1x-npu.rules`) |
| `qualify_n1x_ort.py` | Physical NPU qualification of `N1xInferenceSession` (`mnist-12`, `emotion-ferplus-8`, `super-resolution-10`, and 3-stage `DLA->Host->DLA`) |
| `qualify_npu_reload.py` | Physical startup, cuDLA inference, `REEXEC` reuse, HFRP power-down, unload, and reload qualification |
| `qualify_cudla.py` | Physical cuDLA qualification (`--reuse` exercises `REEXEC_IO` across changing inputs) |

## `Makefile` Test Targets

| Target | Scope | Prerequisites |
| --- | --- | --- |
| `make check` | `check-repo` + `test-portable` (CI default) | Python 3.12 + C compiler |
| `make test-portable` | 16 C/Python unit tests covering ACPI SROOT coherency, HFRP setup/shutdown, VBIOS policy, `/dev/accel` ioctls, `mmap`, statistics, preemption, and reload | Python 3.12 + C compiler |
| `make test-compiler` | Native `t254` compiler (`test_n1x_compiler.py`), ONNX EP (`test_n1x_ort.py`), and super-resolution tiling (`test_superres_image.py`) | `make runtime` |
| `make test-cudla` | `libnvcudla.so` model, API, CUDA staging, and vision partition tests | CUDA driver + cuDLA archive |
| `make module` | Build `n1x_npu_bootstrap.ko` against `KERNEL_RELEASE` | Matching kernel headers |
