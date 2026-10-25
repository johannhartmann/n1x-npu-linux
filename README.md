# N1X NPU Linux driver research

Reverse engineering of the NVIDIA NPU on ASUS ProArt P16 H7607BA to implement
Linux support. **Inference and device bring-up are not implemented yet.** The
current driver components validate the firmware, translate register offsets,
upload firmware words, select/start the RISC-V core, wait for idle without a
start write, and send methods through
caller-provided callbacks. A basic queue-0 task serializer and response capture/ACK helper are also present.
No hardware backend is supplied;
the tests neither bind the ACPI device nor write hardware registers.

## Target selection correction

The original Windows hardware-ID classifier now confirms
`ACPI\VEN_NVDA&DEV_200A&REV_0034 -> 0x254 -> device profile 3 -> DLA HAL 1`.
Its static resources match the P16 candidate ACPI aperture `0x19000000`.
HAL 1 parses a different 66,816-byte RISC-V image with inner header version 2.
The extractor and Linux parser support that image; actual boot is untested.

**The detailed T35x firmware emulations below concern HAL 2, not this HAL-1
image.** They remain reference research and do not validate the P16 firmware's
commands, task ABI, engine registers or instruction opcodes. Those must now be
rechecked against HAL 1. Opaque firmware/boot variants and the live ACPI audit
remain unresolved; no hardware backend is enabled. Basic HAL-1 ping and
mailbox/ACK handling have now been independently revalidated in an RV32 emulator,
including round trips through the Linux C response handler. Four consecutive
commands now use both Linux C method submission and response ACK in one
RV32 emulator instance: the firmware rearms SWGEN1 before each reply. Its
early IRQ initializer also executes without function stubs in RAM, and the
Windows loader's route-mask capture is checked against those firmware outputs.
The raw image also runs from reset to scheduler WFI without function stubs under
an explicit synthetic hardware-handshake model. Four command/ACK round trips
then use its original initialized context and trap handler through `mret`.
The strongest path also executes the original DMA functions: all firmware code
is unchanged, and only register/device effects are modeled. Method FIFO delivery,
machine IRQ entry, DMA copies/completion and reset readiness remain synthetic.
The Linux C task descriptor also reaches the original firmware queue through
this path; an invalid version is rejected without enqueue. Acceptance does not
establish successful execution. A deliberately invalid network now also runs
through the original scheduler: it produces a separate execution-error response,
skips its fence and is dequeued. Physical boot, DMA and IRQ delivery remain untested. The HAL-1 word uploader
also matches Windows across 16,839 callback writes, and its shared core-selection
routine and 78 accessor cases are verified. Physical boot remains unverified. Auxiliary-driver selection is traced below.
HAL-1 common task headers and UMD version strides are now revalidated;
Terminal WRITE_SEM actions are also revalidated, with 256-byte backing required
for HAL-1 cache fetches. Full task/model semantics and instruction execution
remain unverified. HAL-1 cached address-list loading/lookup and instruction-fetch
extents are also checked against the C serializers/validator. The diagnostic
INIT-IL now passes the original HAL-1 network-prefix, slice decoder and opcode
handlers in RAM; real register readiness and task execution remain untested.

The constructor's default remapper is **1**, and the HAL's default firmware-load method is **2**, which uses a separate Windows
driver through IOCTL 0x150214; subtype 1 uses 0x150218. The original policy and
transport were traced up to the OS-call boundary. The word uploader is an
alternate method, not this default. The auxiliary provider is now identified as **nvsrootmbx.sys / NVDA8600 (ROT0)**:
its original registration slice and both IOCTL table entries match. The
callback-only implementation stages the image, builds the separate reinit
request and performs mailbox preparation,
TX/echo and RX. It matches 48 complete Windows wrapper traces, in addition to
individual component and fault-injection checks. The original selector now confirms which opaque variant it requests first under
that default; installed Windows overrides and hardware acceptance remain unknown.
Physical boot orchestration, mapping and DMA lifetime remain pending. See [SROOT transport evidence](docs/sroot-transport.md) for hashes,
extraction, resource boundaries and verification limits. Blob signature and
authentication semantics have not been established.

## Hardware and inputs

- ACPI `NVDA200A`, compatible ID `NVDA2042`, hardware revision `0x34`, `DLA0`.
- P16 BIOS H7607BA.300; device present/enabled and unbound on Linux.
- Historical DSDT resources: MMIO `0x19000000/0x40000`,
  `0x31380000/0x10000`, GSIs 463/800. Live full-table audit is pending.
- Official NVIDIA 617.42 Windows ARM64 package; ASUS INF explicitly maps the
  exact ACPI revision to “NVIDIA NPU”, using the `nvlddmkm` service.
- `nvcudla.dll`: dynamic D3DKMT transport, concrete DeviceKMI interface.
- `nvdla_runtime.dll`: model runtime and built-in diagnostic loadable.
- `nvlddmkm.sys`: NPU kernel implementation and embedded firmware.

See [the reverse-engineering contract](docs/contract.md) for hashes, exact
addresses, proven facts, and remaining bring-up requirements.

## Offline checks

```sh
make test
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python scripts/extract_profile1_firmware.py \
  sources/nvidia/Display.Driver/nvlddmkm.sys sources/dla-profile1-verified.bin
venv/bin/python scripts/extract_firmware.py \
  sources/nvidia/Display.Driver/nvlddmkm.sys sources/dla-t35x-verified.bin
./tests/test_firmware sources/dla-t35x-verified.bin
venv/bin/python scripts/extract_diagnostic.py \
  sources/nvidia/Display.Driver/nvdla_runtime.dll sources/diagnostic-verified.nvda
venv/bin/python scripts/emulate_firmware_parser.py
venv/bin/python scripts/emulate_bitbang_transfer.py
# Extract the opaque default SROOT upload separately from the raw research image:
venv/bin/python scripts/extract_profile1_firmware.py \
  sources/nvidia/Display.Driver/nvlddmkm.sys sources/dla-p16-sroot-v0.bin \
  --sroot-variant 0
# This extracts bytes only; it does not install or boot firmware.
# After extraction, run all vendor-backed differential checks:
make test-vendor
```

The extractor rejects a different KMD hash and refuses to overwrite output.
The emulator runs only the original ARM64 header parser in isolated memory;
logging and memset are stubbed. A separate transfer emulator verifies all
firmware words and callback-error propagation against RAM-only interfaces;
readback verification is stubbed. No Windows driver or firmware is executed on
the machine's devices. The bounded C parser rejects truncated/overflowing and
overlapping sections without modifying its output on failure. The C upload
component matches all 22,039 writes from the original Windows word-transfer
routine. Tests inject write failures and verify that malformed/oversized inputs
produce no writes. The register translator reproduces both recovered Windows mapping tables and
adds alignment/resource bounds checks. The core-control and method-sender components are compared with the original
Windows routines, including busy/error paths. `make kernel-check` compiles all
eight C components against the running kernel without building or loading a
module. The task serializer is checked against the compiled public reference
structure and actual firmware common-header checks. Full task/inference
compatibility is still unverified; no live map selection is assumed. A simulated
round trip runs the original RISC-V ping/response code and the Linux C ACK
handler together, with RAM-only DMA/register/interrupt backends. The original
firmware postaction interpreter is also exercised with WRITE_SEM streams,
alignment variants and DMA failures. A fenced task serializer with logical size 96 and 256-byte backing is checked
against public structures and the original firmware list traversal/interpreter.
Malformed list lengths can write a fence
before returning an error; see the [protocol contract](docs/contract.md).

`inspect_pe.py`, `arm64_string_refs.py`, `msvc_vtables.py`, and
`indirect_slots.py` produce reproducible offline entry-point and dispatch
inventories. Indirect-call matches are candidates, not automatic semantic proof.
`inspect_loadable.py` applies an explicitly provisional v1-schema projection to
the modern diagnostic FlatBuffer; it does not execute the loadable.

## Source acquisition

Vendor packages are kept under ignored `sources/`, never committed:

- [NVIDIA 617.42 RTX Spark Windows ARM64](https://uk.download.nvidia.com/Windows/617.42/617.42-rtx-spark-notebook-win11-arm64-international-whql.exe)
- [ASUS WinRE 1.0.0.521](https://dlcdnets.asus.com/pub/ASUS/nb/Image/CloudReinstallToolEXE/521/MyASUS_in_WinRE_For_USB_V1.0.0.521_1.exe)

ASUS WinRE contains peripheral drivers but no identified NPU driver. The NPU
match is in NVIDIA's display package. Installer programs are unpacked, not run.
Detailed acquisition and analysis outputs are retained locally under `results/`.

NVIDIA-authored reference source is read from the
[linux-nv-oot mirror](https://github.com/OE4T/linux-nv-oot), upstream L4T R38.4
commit `33227347c89543042e0eda352db5de852983c8a3`, especially the RISC-V loader,
AXI/HFRP ports, task descriptors and public ioctl headers. The older Orin
reference is commit `6e071c061fe140e974ef893687f7029ab98376b8`.
These sources are not assumed drop-in compatible with the P16.

The Linux region-record serializer is compared byte-for-byte with the original
Windows ARM64 builder; its output is checked through the original RISC-V
region dispatcher. The supported Printf/Trace layouts do not establish physical
DMA coherency or complete logging support.

The cached address-list path (up to 512 entries) has a Linux serializer with
256-byte padding, verified through the original firmware reader and lookup.
Task descriptors now require a 256-byte-aligned list IOVA. Firmware accepts the
one-past-last index, so strict model index validation remains a required missing
piece before submission.

Setup-command emulation also confirms ABI differences from the public Jetson
header: command 17 enters preemption handling, and region 4 is rejected while
region 6 has a handler. The driver must not copy the reference command enum
unchanged; details and verification limits are in the [contract](docs/contract.md).

A recovered network instruction-list slice now has a Linux index/offset/buffer
validator that accounts for the firmware's full 256-byte fetches, including an
extra fetch after some exact batch boundaries. Its range calculation is checked
against original firmware dispatch and the diagnostic model's INIT_IL field.
Complete instruction/model validation remains unfinished. The actual diagnostic
INIT_IL now runs through the original opcode table and handlers against RAM
registers: three bit-ready polls complete or remain waiting according to the
supplied status. This is not a hardware readiness measurement.

## Before hardware access

See the [candidate ACPI audit](docs/acpi-audit.md), including the important
Windows/P16 address discrepancy. Verify live DSDT and all SSDTs, including
extensions of `DLA0`, parent scopes,
`_PS0/_PS3`, `_PR0/_PR3`, `_DEP`, and referenced PowerResource methods. Do not
assume a successful probe is side-effect free: ACPI power transitions can precede
it. No development module is installed or loaded by these scripts.

After the audit, independently establish the P16 power/reset and RISC-V boot
sequence, bounded DMA addressing, interrupts, firmware ping and version reply.
Only then implement allocation/import, task submission, fences and a numerical
inference test. A build or successful firmware parser test is not device support.
