# Scripts and Test Matrix

## Operational Scripts

| Script | Description |
| --- | --- |
| `check_repo.py` | Validate Markdown links/anchors, Python syntax, JSON receipts, and demo asset hashes |
| `prepare_inputs.py` | Verify (`default`), download (`--fetch`), or extract (`--extract-firmware`) pinned vendor inputs |
| `build_arch_module_package.py` | Build the `n1x-npu-bootstrap-rtx-spark` Arch package (`n1x_npu_bootstrap.ko`, `libn1x_{dla,compiler,ort}.so`, `n1x-{compile,ort-run}`, `n1x-npu.service`, `99-n1x-npu.rules`) |
| `qualify_n1x_ort.py` | Physical NPU qualification of `N1xInferenceSession` (`mnist-12`, `emotion-ferplus-8`, `super-resolution-10`, and 3-stage `DLA->Host->DLA`) |
| `qualify_p16_reload.py` | Multi-cycle physical startup, cuDLA inference, `REEXEC` reuse, HFRP power-down, unload, and reload qualification |
| `qualify_cudla.py` | Physical cuDLA qualification (`--reuse` exercises `REEXEC_IO` across changing inputs) |
| `build_models.py`, `prepare_vision.py`, `build_vision_models.py` | Offline `.nvda` fixture builders for `./demo-superres`, `./demo-denoise`, and `./demo-classify` |
| `install_cudla_broker.py` | Install the confined `n1x-dla-broker` service and fixed cuDLA demo models |

## `Makefile` Test Targets

| Target | Scope | Prerequisites |
| --- | --- | --- |
| `make check` | `check-repo` + `test-portable` (CI default) | Python venv + C compiler |
| `make test` | Native C firmware bounds/header tests (`tests/test_firmware`) | C compiler |
| `make test-portable` | HFRP channel, shutdown, `mmap`, ownership, and reload controller unit tests | Python venv + C compiler |
| `make test-compiler` | Native `t254` compiler (`test_n1x_compiler.py`) and ONNX EP (`test_n1x_ort.py`) suites | `make runtime` |
| `make test-p16-start` | Bootstrap, VBIOS policy, HFRP setup, DMA domain, and `/dev/accel` unit tests | Pinned vendor fixtures |
| `make test-p16-stop` | Windows KMD shutdown emulation and Linux shutdown/transport fault-injection suite | Pinned vendor fixtures |
| `make test-cudla` | `libnvcudla.so` model, API, CUDA staging, and broker security tests | CUDA driver + cuDLA archive |
| `make module` | Build `n1x_npu_bootstrap.ko` and `n1x_npu_observe.ko` against `KERNEL_RELEASE` | Matching kernel headers |
