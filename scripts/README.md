# Scripts and Test Matrix

## Operational Scripts

| Script | Description |
| --- | --- |
| `check_repo.py` | Validate Markdown links/anchors, Python syntax, JSON files, and demo asset hashes |
| `prepare_inputs.py`, `prepare_vision.py` | Verify (`default`), download (`--fetch`), or extract (`--extract-firmware`) pinned vendor inputs and vision checkpoints |
| `build_arch_module_package.py` | Build the `n1x-npu-driver` Arch package (`n1x_npu_bootstrap.ko`, `libn1x_{dla,compiler,ort}.so`, `n1x-{compile,ort-run}`, `99-n1x-npu.rules`) |
| `qualify_n1x_ort.py` | Physical NPU qualification of `N1xInferenceSession` (`mnist-12`, `emotion-ferplus-8`, `super-resolution-10`, and 3-stage `DLA->Host->DLA`) |
| `qualify_npu_reload.py` | Physical startup, cuDLA inference, `REEXEC` reuse, HFRP power-down, unload, and reload qualification |
| `qualify_cudla.py` | Physical cuDLA qualification (`--reuse` exercises `REEXEC_IO` across changing inputs) |
| `replay_windows_startup.py`, `replay_firmware_hfrp.py` | Optional, hash-pinned Unicorn replays of original Windows startup and RV32 firmware client code; private memory only |
| `inspect_windows_stack.py` | Optional, hash-pinned static PE import/export/string inventory of Windows DLA and related platform drivers; no execution |

## `Makefile` Test Targets

| Target | Scope | Prerequisites |
| --- | --- | --- |
| `make check` | `check-repo` + `test-portable` (CI default) | Python 3.12 + C compiler |
| `make test-portable` | C/Python tests covering ACPI SROOT coherency, HFRP startup failure/ownership and cached diagnostics, setup/shutdown, VBIOS policy, `/dev/accel` ioctls, `mmap`, statistics, preemption, and reload | Python 3.12 + C compiler |
| `make research-hfrp` | Execute pinned vendor instructions offline; never part of CI or hardware qualification | `requirements-research.txt` + local verified vendor inputs |
| `make test-compiler` | Native `t254` compiler (`test_n1x_compiler.py`), ONNX EP (`test_n1x_ort.py`), and super-resolution tiling (`test_superres_image.py`) | `make runtime` |
| `make test-cudla` | `libnvcudla.so` model, API, CUDA staging, and vision partition tests | CUDA driver + cuDLA archive |
| `make module` | Build `n1x_npu_bootstrap.ko` against `KERNEL_RELEASE` | Matching kernel headers |
