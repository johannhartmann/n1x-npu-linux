# NVIDIA N1X NPU bring-up on Linux — ASUS ProArt P16

Experimental Linux support for the NVIDIA NPU in the ASUS ProArt P16 H7607BA
(RTX Spark / N1X, BIOS H7607BA.300). The driver and protocol serializers are
reconstructed from the pinned NVIDIA 617.42 Windows ARM64 driver, checked
against original-code emulation and NVIDIA's public Jetson sources.

**The physical P16 executes ReLU, convolution and Conv -> ReLU through NVIDIA's
original Linux cuDLA library, our backend, the Linux driver and the extracted
firmware. Eight changing-input jobs matched independent references.**
This is a manual research driver, not a production replacement for a vendor SDK.

## What this project does

The goal is to make the N1X's dedicated neural-network accelerator usable from
Linux. This work targets NPU/DLA execution, with CUDA buffer and stream
integration for cuDLA applications. It is separate from the broader
[P16 Linux setup](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux)
and the [spark_hwmon telemetry driver](https://github.com/johannhartmann/spark_hwmon).

The implementation reconstructs the startup and task protocols from NVIDIA's
Windows ARM64 binaries. Original driver and RISC-V firmware code is executed
in offline emulators to compare its behavior with the Linux implementation;
public Jetson sources provide additional protocol references. The hardware
test itself runs entirely on Linux and does not require a Windows installation.
The repository contains the Linux module, protocol serializers, analysis tools,
regression tests and the evidence behind the reconstructed interfaces.

The [cuDLA integration research](docs/cudla-call-path.md) identifies a reusable
NVIDIA Linux frontend and its 18-function `libnvcudla.so` backend boundary.
An [experimental N1X backend](docs/cudla-backend.md) now loads supported models
and stages real CUDA buffers through the Linux transport. Physical qualification
covers eight numerical jobs. Additional tests use a modeled NPU boundary for
error injection. Submission currently blocks and attempted kernel DMA backing
remains retained until reboot.

On the tested P16, the Linux module reads the verified board policy, performs
the power/configuration handshake, uploads the extracted firmware, establishes
DMA communication and submits a compiler-produced task. The numerical test
runs ReLU, `output = max(input, 0)`, on 1,568 FP16 values. The device-written
output matches the complete CPU-reference hash. This demonstrates arithmetic
execution beyond device enumeration, a firmware response or a bypass task.

The current scope is one board/BIOS/kernel combination and three checked graphs.
An optional RAWIO-only `/dev/accel` interface and C/Python runtime can submit
prepared images with changing tensors; see [native runtime](docs/native-runtime.md)
for its offline evidence and current limits. The [native loadable loader](docs/loadable-runtime.md)
reads a checked NVDA 0.9.0 subset directly. Broader model compilation/loading
and framework integration remain unfinished. Sustained execution,
interrupt-driven operation, safe resource release, recovery and suspend/resume still need implementation or qualification.
The hardware path is deliberately manual and guarded for the tested machine;
it is not an installer for arbitrary N1X systems. Vendor binaries, firmware,
board ROM captures and generated model artifacts are not included. Reproduction
requires obtaining the pinned inputs described in the linked documentation.

## Verified hardware behavior

On `7.2.8-23-omarchy-p16-asus-npu2`:

| Stage | Observed result |
| --- | --- |
| Board policy | Real BIOS-300 ROM; native parser/initializer match original Windows code |
| HFRP | Handshake, protocol 1.5, eight setup commands and power request accepted |
| SROOT variant 0 | Upload reply `0x18`, status/detail zero; DLA idle observed |
| DMA ping | Host word changes from `0x11111111` to `0x44444444` |
| Region/configuration | Region-5 registered; commands 22, 21 and 23 acknowledged |
| Bypass task | Submission accepted, SWGEN0 observed, fence `0x12345678` written |
| Idle notifications | One regular notification handled in each task path |
| FP16 ReLU | `[1,32,7,7]`; complete 3136-byte output hash matches CPU reference |

The complete sequence passed on 2026-10-27 with the captured board provider
value 1, idle handling enabled and the performance profile. The ReLU test
requires command acceptance, SWGEN0, fence, unchanged input and the expected
output digest. This validates one fixed graph, not arbitrary models or safe
resource teardown. See [the physical record and exact limits](docs/p16-board-start.md).

## Reproduce the manual test

Use the audited `npu2` kernel and its matching headers. Preserve the working
Omarchy boot entry. Vendor binaries, board ROM and generated model artifacts
remain local in ignored `sources/` and `results/`; they are not redistributed.

1. Acquire the pinned Windows inputs and extract SROOT variant 0 as described
   in [the transport contract](docs/sroot-transport.md). The upload file is
   `/usr/lib/firmware/nvidia/n1x/p16-sroot-v0.bin`, 67,508 bytes, SHA256
   `0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4`.
2. Acquire/verify the board ROM using [the bounded read-only procedure](docs/p16-board-start.md).
3. Build the module and run the offline checks:

```sh
make test-p16-start
make -C /lib/modules/7.2.8-23-omarchy-p16-asus-npu2/build \
  M="$PWD/driver" n1x_npu_bootstrap.ko
```

From a fresh boot, with both endpoints unbound and no bootstrap loaded:

```sh
powerprofilesctl set performance
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through bypass_task --log "$PWD/results/NEW-startup.jsonl"
```

The script verifies the machine, module and ROM, selects only the dedicated
DLA/SROOT DMA domains, and runs each explicitly requested startup phase once.
It saves state before and after each operation and stops on the first error.
The kernel pins runtime/module/DMA lifetime after publication, including on
failure. Buffers are not freed or reused, and reset/retry/suspend are not
implemented. **A new attempt requires reboot.** Nothing is enabled at boot,
and the working kernel, UKI and bootloader default are not changed.

For a shorter first prefix use `--through prepare_hfrp`. A successful HFRP-only
run can continue with `--after-hfrp --through bypass_task`; this requires no
previous upload attempt and the same loaded module source version.

## Numerical ReLU probe

The optional probe uses one pinned FP16 ReLU graph of shape `[1,32,7,7]` with
CHW16 input/output. Its backend bytes come from the original Windows compiler;
its CPU reference was independently cross-checked with NumPy and ONNX.
See [compiler reproduction](docs/model-probe.md) for the required local artifacts.

```sh
venv/bin/python scripts/prepare_p16_relu.py
venv/bin/python scripts/test_linux_relu.py
# Fresh boot only; includes all earlier startup phases and the bypass check:
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through relu_probe --log "$PWD/results/NEW-relu.jsonl"
```

The module checks the complete template hash, creates its own DMA addresses,
and requires task acceptance, completion signal, fence, unchanged input and
the expected output digest. A fence alone cannot pass the numerical test.
**Physical ReLU execution passed on 2026-10-27.** Output SHA256:
`6e043c4c2ae0ddfcaca4341ab36f15fdebf066157af5da6483bc56cd6786404f`.
This is one successful execution, not a throughput or sustained-stability benchmark.

## Source and evidence

- [Driver and serializer code](driver/), [interfaces](include/), [tests/tools](scripts/).
- [Current board startup and recovery constraints](docs/p16-board-start.md).
- [Native NVDA loadable ingestion and metadata reverse engineering](docs/loadable-runtime.md).
- [Experimental native accel interface and C/Python runtime](docs/native-runtime.md).
- [DLA integration analysis and recommended Linux API architecture](docs/dla-integration-design.md).
- [ACPI power/resource audit](docs/acpi-audit.md), [Linux/DMA backend](docs/linux-backend.md).
- [HFRP protocol](docs/hfrp-irq.md), [VBIOS reverse engineering](docs/hfrp-vbios.md).
- [SROOT transport](docs/sroot-transport.md), [vendor envelopes](docs/sroot-envelope.md).
- [Task/command contracts](docs/contract.md), [region-5](docs/region5.md).
- [Windows KMD task submission and completion semaphore construction](docs/windows-kmd-task.md).
- [Model/compiler probe](docs/model-probe.md), [boot logging](docs/boot-logging.md).
- [Convolution and Conv → ReLU compiler/runtime support](docs/convolution-compiler.md)
  (constant and intermediate-memory loading and numerical execution of both graphs are physically verified through cuDLA).

Detailed research documents retain earlier unsuccessful experiments and their
limitations. The physical result above supersedes their earlier boot-status
statements. HAL-2/T35x studies are reference material; the P16 uses HAL 1.

`make test` runs the native parser checks. `make test-vendor` additionally needs
the pinned local vendor inputs, analysis venv and ACPICA `iasl`. `make test-relu`
requires the compiled fixture. These offline commands do not access NPU
hardware or load modules. `make kernel-check` builds callback components only.
