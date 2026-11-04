# Scripts and Test Matrix

## Operational Scripts

| Script | Description |
| --- | --- |
| `check_repo.py` | Validate Markdown links/anchors, Python syntax, JSON receipts, and demo asset hashes |
| `prepare_inputs.py`, `prepare_vision.py` | Verify (`default`), download (`--fetch`), or extract (`--extract-firmware`) pinned vendor inputs and vision checkpoints |
| `build_arch_module_package.py` | Build the `n1x-npu-bootstrap-rtx-spark` Arch package (`n1x_npu_bootstrap.ko`, `libn1x_{dla,compiler,ort}.so`, `n1x-{compile,ort-run}`, `n1x-npu.service`, `99-n1x-npu.rules`) |
| `check_update_readiness.py` | Read-only system readiness check after kernel or OS updates |
| `qualify_n1x_ort.py` | Physical NPU qualification of `N1xInferenceSession` (`mnist-12`, `emotion-ferplus-8`, `super-resolution-10`, and 3-stage `DLA->Host->DLA`) |
| `qualify_p16_reload.py`, `qualify_p16_start.py` | Physical startup, cuDLA inference, `REEXEC` reuse, HFRP power-down, unload, and reload qualification |
| `qualify_cudla.py` | Physical cuDLA qualification (`--reuse` exercises `REEXEC_IO` across changing inputs) |
| `install_cudla_broker.py` | Install the confined `n1x-dla-broker` service and fixed cuDLA demo models |

## `Makefile` Test Targets

| Target | Scope | Prerequisites |
| --- | --- | --- |
| `make check` | `check-repo` + `test-portable` (CI default) | Python 3.12 + C compiler |
| `make test-portable` | 19 C/Python unit tests covering ACPI SROOT coherency, HFRP setup/shutdown, VBIOS policy, `/dev/accel` ioctls, `mmap`, statistics, preemption, and reload | Python 3.12 + C compiler |
| `make test-compiler` | Native `t254` compiler (`test_n1x_compiler.py`), ONNX EP (`test_n1x_ort.py`), and super-resolution tiling (`test_superres_image.py`) | `make runtime` |
| `make test-cudla` | `libnvcudla.so` model, API, CUDA staging, vision partitions, and broker security tests | CUDA driver + cuDLA archive |
| `make module` | Build `n1x_npu_bootstrap.ko` against `KERNEL_RELEASE` | Matching kernel headers |
