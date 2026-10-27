# NVIDIA N1X NPU bring-up on Linux — ASUS ProArt P16

Experimental Linux support for the NVIDIA NPU in the ASUS ProArt P16 H7607BA
(RTX Spark / N1X, BIOS H7607BA.300). The driver and protocol serializers are
reconstructed from the pinned NVIDIA 617.42 Windows ARM64 driver, checked
against original-code emulation and NVIDIA's public Jetson sources.

**Firmware startup and a bypass task work on the physical P16. Numerical
inference and a general `/dev/accel` API are not yet verified or provided.**
This is a manual research driver, not a production replacement for a vendor SDK.

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

The first successful run used the older command-23 fallback provider value 4.
The captured board value 1 was also accepted on the next boot. That run
stopped before task submission because a regular idle notification was pending.
The driver now handles that notification before and during tasks; its hardware
retest and the optional numerical ReLU probe remain pending. See
[the physical record and exact limits](docs/p16-board-start.md).

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
This path is implemented and tested offline; **physical ReLU execution is pending**.

## Source and evidence

- [Driver and serializer code](driver/), [interfaces](include/), [tests/tools](scripts/).
- [Current board startup and recovery constraints](docs/p16-board-start.md).
- [ACPI power/resource audit](docs/acpi-audit.md), [Linux/DMA backend](docs/linux-backend.md).
- [HFRP protocol](docs/hfrp-irq.md), [VBIOS reverse engineering](docs/hfrp-vbios.md).
- [SROOT transport](docs/sroot-transport.md), [vendor envelopes](docs/sroot-envelope.md).
- [Task/command contracts](docs/contract.md), [region-5](docs/region5.md).
- [Model/compiler probe](docs/model-probe.md), [boot logging](docs/boot-logging.md).

Detailed research documents retain earlier unsuccessful experiments and their
limitations. The physical result above supersedes their earlier boot-status
statements. HAL-2/T35x studies are reference material; the P16 uses HAL 1.

`make test` runs the native parser checks. `make test-vendor` additionally needs
the pinned local vendor inputs, analysis venv and ACPICA `iasl`. `make test-relu`
requires the compiled fixture. These offline commands do not access NPU
hardware or load modules. `make kernel-check` builds callback components only.
