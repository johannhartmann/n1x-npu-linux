# Scripts and Test Matrix

## Operational Scripts

| Script | Description |
| --- | --- |
| `check_repo.py` | Validate Markdown links/anchors, Python syntax, JSON receipts, and demo asset hashes |
| `prepare_inputs.py`, `prepare_vision.py` | Verify (`default`), download (`--fetch`), or extract (`--extract-firmware`) pinned vendor inputs and vision checkpoints |
| `build_arch_module_package.py` | Build the `n1x-npu-bootstrap-rtx-spark` Arch package (`n1x_npu_bootstrap.ko`, `libn1x_{dla,compiler,ort}.so`, `n1x-{compile,ort-run}`, `n1x-npu.service`, `99-n1x-npu.rules`) |
| `qualify_n1x_ort.py` | Physical NPU qualification of `N1xInferenceSession` (`mnist-12`, `emotion-ferplus-8`, `super-resolution-10`, and 3-stage `DLA->Host->DLA`) |
| `qualify_p16_reload.py` | Multi-cycle physical startup, cuDLA inference, `REEXEC` reuse, HFRP power-down, unload, and reload qualification |
| `qualify_cudla.py`, `qualify_vision_examples.py` | Physical cuDLA and vision demo qualification (`--reuse` exercises `REEXEC_IO` across changing inputs) |
| `install_cudla_broker.py` | Install the confined `n1x-dla-broker` service and fixed cuDLA demo models |

## `Makefile` Test Targets

| Target | Scope | Prerequisites |
| --- | --- | --- |
| `make check` | `check-repo` + `test-portable` (CI default) | Python venv + C compiler |
| `make test` | Native C firmware bounds/header tests (`tests/test_firmware`) | C compiler |
| `make test-portable` | 18 C/Python unit tests covering HFRP setup/shutdown, VBIOS policy, `/dev/accel` ioctls, `mmap`, statistics, preemption, and reload | Python venv + C compiler |
| `make test-compiler` | Native `t254` compiler (`test_n1x_compiler.py`) and ONNX EP (`test_n1x_ort.py`) suites | `make runtime` |
| `make test-cudla` | `libnvcudla.so` model, API, CUDA staging, and broker security tests | CUDA driver + cuDLA archive |
| `make module` | Build `n1x_npu_bootstrap.ko` against `KERNEL_RELEASE` | Matching kernel headers |
