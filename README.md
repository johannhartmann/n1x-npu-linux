# N1X NPU Linux driver research

Reverse engineering of the NVIDIA NPU on ASUS ProArt P16 H7607BA to implement
Linux support. **Physical firmware boot and inference are not verified.** The
current driver components validate the firmware, translate register offsets,
upload firmware words, select/start the RISC-V core, wait for idle without a
start write, and send methods through
caller-provided callbacks. A basic queue-0 task serializer and response capture/ACK helper are also present.
Region-5 records and local ring setup are now verified against the raw HAL-1
firmware; this region is unsupported in the separate HAL-2 reference image.
A native C response-field decoder is checked against the original Windows parser;
it preserves unknown bits and does not infer task completion. The original
Windows IRQ callback is also exercised for error-report ordering and SWGEN0
counter handling; report/recovery boundaries remain synthetic.
An explicit-load Linux observation module now validates the audited P16
resources and exposes DMA/IOMMU state. It has no write path; optional status
reads require a load-time flag. A separate bootstrap experiment is described below;
full boot initialization and inference are not implemented.
The isolated `7.2.8-23-omarchy-p16-asus-npu1` kernel has now booted on the
machine. Both dedicated IOMMU groups were switched to DMA domains, and the
observation module successfully allocated, CPU-checked and freed buffers for
both devices. SROOT now has the intended noncoherent DMA setup. This is a
physical allocation result, not a device transfer or firmware-start result;
see the [live qualification record](docs/linux-backend.md#live-test-kernel-and-allocation-result).
The initial method-2 upload phase connects SROOT exchange, firmware status
checking and the write-free DLA idle wait. A separate, manual bootstrap module
now connects that phase to guarded Linux MMIO and SROOT DMA storage. It builds
for the isolated test kernel. Its first physical upload received SROOT reply
`0x2a006018`: command `0x18`, status `6`, additional byte `0x2a`, no payload.
The original Windows decoder confirms those fields; their error meanings
remain unresolved. The DMA buffer is retained, with no automatic retry.
After reboot, one variant-1 upload was also rejected, with reply `0x253ff018`
(status `1023`, detail `37`, no payload). Its meanings remain unknown too.
The current bootstrap retains that allocation, and no later NPU phase ran.
The initial host runtime-PM preflight defect was corrected before that attempt.
This remains a one-attempt experiment requiring retained DMA storage/reboot, and does not
complete IRQ/configuration initialization or inference. An additional opt-in
DMA-word ping, exact Windows-layout region-5 setup, command-22 export and
Windows-default command-21 import are implemented but remain untested on hardware.
Custom configuration overrides and ring consumption are not exposed. See
[default configuration](docs/default-config.md). A standalone recent-history reader works
on immutable CPU snapshots; see [history evidence](docs/region5-history.md) and
[region-5 setup](docs/region5.md).
The auxiliary HFRP register/IRQ capture and shared Windows dispatch are now
cross-checked against L4T. Bounded HFRP command/reply ring helpers are compared
against the original Windows code but are not connected to the live modules;
physical IRQ roles and the Linux handshake remain unverified. See
[HFRP/IRQ evidence](docs/hfrp-irq.md).
See [Linux backend](docs/linux-backend.md). Offline tests neither bind the ACPI
device nor write hardware registers.

## Target selection correction

The original Windows hardware-ID classifier now confirms
`ACPI\VEN_NVDA&DEV_200A&REV_0034 -> 0x254 -> device profile 3 -> DLA HAL 1`.
Its static resources match the P16 candidate ACPI aperture `0x19000000`.
HAL 1 parses a different 66,816-byte RISC-V image with inner header version 2.
The extractor and Linux parser support that image; actual boot is untested.

**The detailed T35x firmware emulations below concern HAL 2, not this HAL-1
image.** They remain reference research and do not validate the P16 firmware's
commands, task ABI, engine registers or instruction opcodes. Those must now be
rechecked against HAL 1. Opaque firmware/boot variants and physical boot remain unresolved. The live
ACPI audit is complete; the observation module has no write backend. Basic HAL-1 ping and
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
skips its fence and is dequeued. The original diagnostic network/instruction
blobs reach INIT-IL through this same path, under explicit synthetic readiness
values. A second fixture supplies all nine projected diagnostic allocations
and reaches the first engine-wait point, with work still active and no simulated
compute completion. Physical boot, DMA and IRQ delivery remain untested. The HAL-1 word uploader
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
- P16 BIOS H7607BA.300; device present/enabled. The explicit observation
  driver can bind it; bootstrap/IRQ behavior remains unqualified.
- Historical DSDT resources: MMIO `0x19000000/0x40000`,
  `0x31380000/0x10000`, GSIs 463/800. The live full-table audit confirms these resources.
- Official NVIDIA 617.42 Windows ARM64 package; ASUS INF explicitly maps the
  exact ACPI revision to “NVIDIA NPU”, using the `nvlddmkm` service.
- `nvcudla.dll`: dynamic D3DKMT transport, concrete DeviceKMI interface.
- `nvdla_runtime.dll`: model runtime and built-in diagnostic loadable.
- `nvlddmkm.sys`: NPU kernel implementation and embedded firmware.

See [the reverse-engineering contract](docs/contract.md) for hashes, exact
addresses, proven facts, and remaining bring-up requirements.

## Offline checks

The complete vendor-backed suite also requires ACPICA `iasl` for the read-only
ACPI snapshot workflow checks. See [the live-table procedure](docs/acpi-audit.md).

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
callback C components against the running kernel without linking or loading a
module. `KERNEL_RELEASE=<installed-release>` explicitly selects another installed
header tree for both `make kernel-check` and `make module`; the default remains
the running kernel. See [bootstrap rebuild instructions](docs/linux-backend.md#rebuilding-the-current-bootstrap-from-source). The task serializer is checked against the compiled public reference
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


The explicit Linux bootstrap also offers a one-attempt [Windows-fallback
LPWR configuration import](docs/linux-backend.md#optional-windows-fallback-lpwr-configuration-import-built-not-yet-run)
after default config import. Its payload matches the original Windows builder
and roundtrips through unmodified raw HAL-1 code under modeled DMA/IRQ effects.
It remains off by default and physically untested; this is not a working
inference or power-management driver.


A [single-task bypass descriptor](docs/bypass-task.md) now exercises the original
raw HAL-1 scheduler's successful fence/dequeue/SWGEN0 path in emulation, alongside
execution-error, postaction-error and descriptor-rejection controls. It is a
completion-transport qualification primitive and performs no inference. The
bootstrap now exposes it through an optional root-only one-attempt trigger for
one internally built descriptor, with acceptance/error/fence/SWGEN0 checks and
retained DMA backing. It accepts no arbitrary model/task pointers.


The [builtin Windows diagnostic audit](docs/diagnostic-contract.md) executes
its original runtime selector, verifies the actual embedded pointer/hash, and
maps the observed named buffers. Two original diagnostic IL blocks now prove a four-byte register copy and
zero/nonzero software predicates. The remaining result layout and hardware
comparison meaning are still unresolved; neither a zero result buffer nor
whole-buffer comparison with the blob named positive is treated as success.

A [small FP16 ReLU source model and CPU oracle](docs/model-probe.md) now provide
a numerical target for investigating the Windows ARM TensorRT 11.4 compiler.
The original Windows backend now completes pretranslation and translation of
captured native ReLU intermediates in Unicorn. Replaying its actual
pretranslation changes into the native frontend exposes the distinct second
handoff, including input/output address-list indices. Using that handoff
produces a 30,464-byte instruction blob, data, network descriptor, preemption
context and metadata with tensor bindings. Original native getters copy the
nonempty execution blobs through an explicit ABI adapter. The earlier test
reused the pretranslation descriptor and produced an empty instruction blob;
it remains a diagnostic comparison, not the final compilation handoff.
The native serializer now packages the exact Windows results into a checked
2,800,596-byte NVDA 0.9.0 artifact, including the task/address lists and all five
execution/metadata blobs. This is a reproducible adapter for one ReLU fixture,
not a native compiler backend. The original Windows schema-validation prefix,
content-version provider/gate and content materializer now accept this fixture
in isolation. A separate probe runs the original constructor and complete outer
deserializer with original C++ containers, checking all task, memory, address,
content and tensor records. Required original CRT/regex initialization now
exposes a statistics-header mismatch: compiler byte 2 versus runtime 617.42's
expected byte 1. The outer deserializer ignores that error. A version-only
mutation makes the old runtime index 3,392 of 28,736 bytes, so it is not a
compatibility fix. A [Linux statistics-layout parser](docs/statistics-v2.md)
now checks the newer header and record bounds against the original compiler
builder; metadata fields and hardware counters remain opaque. Malformed-reference tests also show that deserialization
alone does not establish model validity. A complete original memory-initialization
probe now resolves the five named execution blobs through the original content
map/getter, passes the original version gates and copies all 2,798,720 bytes
unchanged. Missing names and short destinations are rejected, but a later
failure leaves earlier copies in place. The native C content initializer now
checks all selected names and logical destination bounds before any write,
then copies in the original order. Its buffers feed the firmware test directly.
These are still private RAM allocations, not device allocations.
Native C address resolution checks
selected model references, preserves logical versus mapped extents, and feeds
the C wire-list serializer used by the firmware test. The unmodified HAL-1 firmware processes its real instruction
and address buffers and completes the original fence/IRQ path when given a
modeled engine-completion status. Output remains poisoned because arithmetic
is not emulated. Complete runtime initialization, physical firmware execution
and verified Linux NPU inference remain unresolved. Run `make test-relu` after
generating the artifacts described in the model-probe guide.


The default SROOT image now has an [offline signed-envelope audit](docs/sroot-envelope.md):
its embedded-key signature and both complete body digests verify. Chip trust,
physical loading and equivalence to the separately emulated raw HAL-1 code
remain unproved; the bootstrap's exact whole-image hash policy is unchanged.

The [Windows fallback image](docs/sroot-envelope.md#windows-fallback-contains-the-complete-raw-hal-1-firmware)
now provides a direct link to that raw code: remapper-1 variant 1 contains the
complete unmodified HAL-1 image at offset 692. All four vendor upload signatures
and body digests verify offline. A rebuilt bootstrap can explicitly select
this separately hash-pinned image with `firmware_variant=1`; it still permits
only one upload per boot. Its first physical attempt was rejected; see the
[latest hardware result](docs/linux-backend.md#latest-hardware-result-variant-1-rejected).


The diagnostic scheduler audit exposes an unresolved [network-extension
contract](docs/diagnostic-contract.md#scheduler-record-order-and-zero-extension-boundary):
raw firmware reads IRQ fields beyond the blob's 128 initialized network bytes.
The fixture's zero extension does not make progress under original-handler IRQ
injections. Runtime materialization/ABI compatibility must be resolved before
this fixture can establish a completed diagnostic; no guessed suffix is used.

A [DGX Spark OSROOT research input](docs/spark-osroot-research.md) now provides
manufacturer-distributed RISC-V mailbox/authentication code with a verified
internal signature and component hashes. It is an offline reference for the
unresolved SROOT replies; no P16 compatibility or error-code mapping is claimed.
