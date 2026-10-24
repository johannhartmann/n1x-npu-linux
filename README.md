# N1X NPU Linux driver research

Reverse engineering of the NVIDIA NPU on ASUS ProArt P16 H7607BA to implement
Linux support. **Inference and device bring-up are not implemented yet.** The
current driver components validate the firmware, translate register offsets,
upload firmware words, select/start the RISC-V core and send methods through
caller-provided callbacks. A basic queue-0 task serializer and response capture/ACK helper are also present.
No hardware backend is supplied;
the tests neither bind the ACPI device nor write hardware registers.

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
venv/bin/python scripts/extract_firmware.py \
  sources/nvidia/Display.Driver/nvlddmkm.sys sources/dla-t35x-verified.bin
./tests/test_firmware sources/dla-t35x-verified.bin
venv/bin/python scripts/extract_diagnostic.py \
  sources/nvidia/Display.Driver/nvdla_runtime.dll sources/diagnostic-verified.nvda
venv/bin/python scripts/emulate_firmware_parser.py
venv/bin/python scripts/emulate_bitbang_transfer.py
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
seven C components against the running kernel without building or loading a
module. The task serializer is checked against the compiled public reference
structure and actual firmware common-header checks. Full task/inference
compatibility is still unverified; no live map selection is assumed. A simulated
round trip runs the original RISC-V ping/response code and the Linux C ACK
handler together, with RAM-only DMA/register/interrupt backends. The original
firmware postaction interpreter is also exercised with WRITE_SEM streams,
alignment variants and DMA failures. A 96-byte fenced task serializer is checked
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

Setup-command emulation also confirms ABI differences from the public Jetson
header: command 17 enters preemption handling, and region 4 is rejected while
region 6 has a handler. The driver must not copy the reference command enum
unchanged; details and verification limits are in the [contract](docs/contract.md).

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
