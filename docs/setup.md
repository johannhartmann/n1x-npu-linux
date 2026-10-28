# Setup and reproduction

This guide starts from a fresh checkout on an already enabled ARM64 P16.
It builds research tools and artifacts locally. Hardware commands appear only
in the final section and must be run explicitly after reviewing the results.

## 1. Platform prerequisites

The physical driver currently requires ASUS ProArt P16 H7607BA, BIOS
`H7607BA.300`, and kernel **`7.2.8-23-omarchy-p16-asus-npu2`** with matching
headers. Start with the separate [P16 Linux setup](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux).
This repository does not supply a generic Omarchy kernel installer or signed
kernel package. The exact test kernel and scoped noncoherent SROOT DMA patch
are documented in [Linux backend](linux-backend.md). Earlier kernel-build and
FRTS/UKI experiments are indexed as historical workflows in [the tool index](../scripts/README.md).

Preserve the known-working boot entry when preparing a test kernel. A different
kernel release is rejected by the bootstrap; removing that guard does not
establish compatibility. No command below changes a bootloader entry.

Tool dependencies on an Arch/Omarchy ARM64 host are a C/C++ toolchain, make,
binutils, Python, `uv`, bubblewrap, GDB, OpenSSL development headers, libdrm
headers and ACPICA `iasl`. A 7-Zip CLI can unpack the vendor installer. The
cuDLA backend additionally needs the installed CUDA toolkit (`/opt/cuda` by
default, including `cuda.h` and its driver stub) and the real NVIDIA CUDA
driver at runtime. Install matching kernel headers separately.

## 2. Checkout and Python environments

```sh
git clone https://github.com/johannhartmann/n1x-npu-linux.git
cd n1x-npu-linux
uv venv --python 3.12 venv
uv pip install --python venv/bin/python -r requirements.txt -r requirements-probe.txt
mkdir -p sources results
make test-portable
```

`make test` needs only a C compiler. `make test-portable` additionally uses the
analysis environment and modeled OS/transport boundaries, with no vendor
files, GPU, root or kernel tree. The native compiler stage needs ARM64 and
unprivileged bubblewrap namespaces; do not replace its sandbox with root.

The optional image/reference environment is separate:

```sh
uv venv --python 3.12 sources/superres-venv
uv pip install --python sources/superres-venv/bin/python -r requirements-superres.txt
```

## 3. Acquire the pinned inputs

The input helper downloads the public cuDLA archive, native compiler Debian
package, super-resolution ONNX model and optional NASA sample photograph. It
also invokes the existing range-based Windows compiler extractor. Hashes are
checked before use; existing mismatched files are refused. Packages are
unpacked into ignored directories and are not installed system-wide.

```sh
venv/bin/python scripts/prepare_inputs.py --fetch
```

An initial nonzero result is expected while manually supplied Windows-driver
files or extracted firmware are still missing; all missing paths are listed.
Re-running `--fetch` reuses verified inputs. Plain `prepare_inputs.py` only
checks hashes and never downloads or extracts anything.

Public package sources are NVIDIA's [cuDLA SBSA archive index](https://developer.download.nvidia.com/compute/cuda/redist/libcudla/linux-sbsa/)
and [Debian SBSA package index](https://developer.download.nvidia.com/compute/cuda/repos/debian12/sbsa/).
The [Windows compiler acquisition](model-probe.md#native-compiler-counterpart-acquired)
and [model research](github-demo-research.md) record the remaining source context.

Supply **NVIDIA 617.42 Windows ARM64** driver files separately. Obtain the
matching vendor package and unpack it without running its installer, for example:

```sh
7z x /absolute/path/to/NVIDIA-617.42-ARM64.exe -osources/nvidia 'Display.Driver/*'
```

The installer previously used has SHA-256
`425a75f54655ddcc8ee6859cec49bc431a17ef18094daaf59f205b159fb6e2bd`.
The helper checks the extracted files, so it also accepts an equivalent
extraction from another vendor container with identical contents:

| Required path | SHA-256 |
| --- | --- |
| `sources/nvidia/Display.Driver/nvlddmkm.sys` | `73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987` |
| `sources/nvidia/Display.Driver/nvdla_runtime.dll` | `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b` |

Extract and verify the local firmware artifacts:

```sh
venv/bin/python scripts/prepare_inputs.py --extract-firmware
venv/bin/python scripts/prepare_inputs.py
```

This creates the HAL-1/HAL-2 research images and SROOT variants 0/1, without
hardware access. Only HAL 1 and remapper-1 SROOT variant 0 are used by the P16
startup below; the other images support existing offline regression tests.
Additional vendor-runtime, auxiliary-driver or ACPI inputs are needed for
some historical research suites, as their individual documents explain.

## 4. Compile the checked models offline

On a fresh workspace with the inputs above:

```sh
venv/bin/python scripts/build_models.py --superres
```

The helper constructs ReLU, Conv, Conv → ReLU and the trained super-resolution
graph. Native compiler calls run in the existing device-free bubblewrap
sandbox; Windows backend passes execute under Unicorn. Original packed
constants are replayed before final serialization. Every final loadable hash
must match the checked artifact. Omit `--superres` for the three small fixtures.

The [offline reproduction receipt](offline-reproduction-2026-10-28.json) records
a clean source export with no pre-existing model/capture outputs, Python 3.12
and all four expected loadable hashes. It does not establish physical execution.

This is a fixture compiler workflow, not a general ONNX-to-N1X compiler. It
refuses existing output directories to preserve previous captures and qualified
artifacts. Use a separate checkout for a rebuild; do not delete evidence from
an unsuccessful hardware run. Commands and complete compiler output are saved
in `results/model-build.jsonl` and `results/model-build.log`.

The generated paths include `results/native-compile-t254/relu.nvda`, the
corresponding `-conv1x1`, `-conv1x1_relu` and `-superres` directories,
`results/relu-probe/` and `results/p16-relu-template.bin`.

```sh
make runtime cudla
make test-cudla
sources/superres-venv/bin/python scripts/test_superres_image.py
```

`make test-cudla` uses the real installed CUDA driver but a modeled NPU
transport; it does not start DLA firmware. The image test uses the CPU ONNX
reference and likewise makes no claim of physical NPU execution.

## 5. Board input and module build

Use the [bounded read-only board ROM procedure](p16-board-start.md#reproducible-read-only-acquisition)
to create `results/p16-bios300-physical.rom`. That explicitly privileged
acquisition is separate from setup and must match SHA-256
`a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c`.
An already acquired, identical capture may be copied into that path. The
startup qualifier verifies it and installs it locally when absent.

Install the checked SROOT image into the firmware search path:

```sh
sudo install -Dm600 sources/dla-p16-sroot-v0.bin /usr/lib/firmware/nvidia/n1x/p16-sroot-v0.bin
sudo install -Dm600 sources/dla-p16-sroot-v1.bin /usr/lib/firmware/nvidia/n1x/p16-sroot-v1.bin
venv/bin/python scripts/verify_p16_board_policy.py results/p16-bios300-physical.rom
make test-p16-stop
make module
```

These `make` targets do not load the module or boot firmware. Ensure the
installed kernel/header pair is the exact audited release. The ReLU template
and board capture are installed by the startup qualifier after hash checks.

## 6. Explicit hardware qualification after reboot

Replacing an older module without shutdown support, or an instance whose stop
has failed, requires a reboot of the **same audited npu2 kernel**. Keep the
known-working boot entry.
Then, when ready to run the bounded qualification (the current power profile is recorded and left unchanged):

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_p16_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

The output directory must be new. The qualifier starts firmware when needed,
runs baseline and same-model changing-input jobs with allocation checks,
stops and releases DMA, tests module ownership, unloads,
reloads and verifies inference again. Logs are durable and the first failure
stops the sequence. [Shutdown/reload](shutdown-reload.md) gives the exact contract.
Qualifiers record the selected platform profile without changing it. No service
or timer is installed to enforce a profile.

Only after successful reload and reuse qualification, run the
[full-image demo](../demo/README.md). Its complete physical 512x512 and
1024x1024 runs remain pending. See [status](status.md) before making broader
hardware claims. Setup, model generation and offline tests do not establish
successful physical boot, teardown or inference by themselves.
