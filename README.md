# NVIDIA N1X NPU on Linux — ASUS ProArt P16 / RTX Spark

Experimental Linux support for the dedicated NVIDIA NPU/DLA in the **ASUS
ProArt P16 H7607BA (RTX Spark / N1X, BIOS H7607BA.300)**. Reconstructed from
NVIDIA’s Windows ARM64 driver, checked against original-code emulation and
public Jetson sources, and tested on the physical P16 entirely under Linux.

**Real inference works through NVIDIA’s original Linux cuDLA library → our
backend → Linux driver → extracted DLA firmware.** Eight changing-input ReLU,
convolution and Conv → ReLU jobs matched independent references exactly.
The current build also passed two physical unload/reload cycles and complete
512→1536 and 1024→3072 super-resolution image runs.

This is a manually operated research driver for one audited board/BIOS/kernel
combination. General model compilation, automatic suspend/resume and production
deployment remain outside the verified scope. A Windows installation is not
required; pinned Windows driver files are used for analysis and extraction.

## Start here

- **[Setup and reproduction](docs/setup.md):** fresh checkout, dependencies,
  pinned inputs, model compilation and explicit hardware commands.
- **[Current status](docs/status.md):** physical measurements, offline evidence
  and remaining limits.
- **[Super-resolution demo](demo/README.md):** full-image 3x pipeline;
  complete 512→1536 and 1024→3072 image runs verified on the P16.
- **[cuDLA backend](docs/cudla-backend.md):** API contract, staging and limits.
- **[Shutdown and reload](docs/shutdown-reload.md):** cleanup protocol and
  post-reboot qualification.
- **[Research documentation](docs/README.md)** and **[tool index](scripts/README.md)**.

## What is verified

The tested kernel is `7.2.8-23-omarchy-p16-asus-npu2`, with matching headers
and the recorded power profile. Board/BIOS/kernel checks are enforced in code.
Tests preserve the current power profile.

| Capability | Evidence | Status |
| --- | --- | --- |
| Board policy, HFRP, firmware upload and DMA/config setup | Original-code comparison and physical startup | Verified on P16 |
| cuDLA ReLU, 1x1 Conv, Conv → ReLU | Eight changing-input jobs, exact reference matches | Verified on P16 |
| Trained super-resolution graph | Complete 512x512 and 1024x1024 DLA runs; independent ONNX reference | Verified on P16 |
| Full-image tiling and borders through 1024x1024 → 3072x3072 | 461 physical DLA windows plus whole-image CPU reference | Verified on P16 |
| Completed-task I/O reuse | Changing-input cuDLA fixtures and one allocation per complete image | Verified on P16 |
| Checked shutdown, DMA cleanup and module reload | Two physical cycles: zero retained backing, ordinary unload, restart and numerical checks | Verified on P16 |
| Recovery from uncertain DMA/firmware failure | Buffers and lifetime references remain pinned | Reboot required |
| Automatic startup, suspend/resume, arbitrary TensorRT engines | Outside the implemented/verified contract | Unsupported |

The [cuDLA receipt](docs/cudla-hardware-2026-10-28.json) identifies exact software,
firmware, model and output hashes for the eight-job physical test. The
[current hardware receipt](docs/npu-hardware-2026-10-28.json) records 48 fixture
jobs, two reload cycles and both full-image runs.
[Status](docs/status.md) gives the remaining limits.

## How it works

```text
cuDLA application + CUDA buffers
             │
NVIDIA’s original libcudla.so.1
             │
this repository’s libnvcudla.so
             │ blocking CUDA staging
RAWIO-only Linux accel transport
             │
Linux driver → extracted firmware → physical N1X DLA
```

Learned inference runs on DLA. CPU code handles staging, image preparation and
independent validation. Models must be trusted: bounds-checked metadata is not
an instruction sandbox, and the transport requires `CAP_SYS_RAWIO`.

The kernel retains submitted DMA after client close or model unregister.
The new code can reuse successful-task I/O and release backing after checked
explicit shutdown; both paths have passed the bounded P16 qualification.
Uncertain execution or shutdown errors keep DMA, power and module references pinned until reboot.
There are no automatic retries or forced-unload recovery.

## Offline checks

A C compiler suffices for the smallest check; it needs no vendor files, GPU,
root or kernel headers:

```sh
make test
```

After installing the analysis requirements from [setup](docs/setup.md), run
fixture-free protocol/lifetime checks with:

```sh
make test-portable
```

Additional test targets and their inputs are listed in the [tool index](scripts/README.md).
`make module` only builds; firmware startup and hardware qualification are
separate, explicit operations.

## Source layout

| Path | Purpose |
| --- | --- |
| `driver/`, `include/`, `patches/` | Linux implementation, protocols and scoped kernel patches |
| `runtime/` | cuDLA backend, loadable materializers and application bindings |
| `demo/` | Experimental full-image super-resolution application |
| `scripts/`, `tests/` | Reproduction, original-code emulation and regression tests |
| `docs/` | Current guides, qualification receipts and historical research |
| `sources/`, `results/`, `venv/` | Ignored local inputs, outputs and environments |

Vendor binaries, firmware, board ROM captures and generated loadables are not
distributed in Git. [Setup](docs/setup.md) explains acquisition and generation.
Historical research retains its unsuccessful experiments and dated limitations;
[status](docs/status.md) is the current summary.

Related projects: [P16 Linux setup](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux)
and [spark_hwmon telemetry](https://github.com/johannhartmann/spark_hwmon).

## Licensing

Kernel components use GPL-2.0-only; userspace and analysis components use MIT
as mapped in [LICENSING.md](LICENSING.md). File-level SPDX notices take precedence.
See [NOTICE](NOTICE) for external inputs and attribution.
