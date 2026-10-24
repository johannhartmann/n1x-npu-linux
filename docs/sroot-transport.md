# NPU boot provider: SROOT mailbox

The default NPU boot path reaches **nvsrootmbx.sys**, not merely another entry
point inside nvlddmkm. This is established by matching interface registration
and IOCTL records, not by guessing from driver names.

## Verified chain

1. nvlddmkm RVA `0xd3f774` calls `0xd3b008` and stores the result in outer
   device +0x308. That opener calls IoGetDeviceInterfaces with GUID
   `94f86b15-22a9-4607-b95b-3b05184ae8f7`, then RtlInitUnicodeString and
   IoGetDeviceObjectPointer. Import identities are read from its PE table.
2. The default method-2 wrapper `0xd49ef0` passes this handle to `0xd3bbc8`.
   Its normal request is IOCTL `0x150214`, with the firmware blob as input.
   Subtype 1 sends `0x150218` and the eight-byte reinitialization request.
3. The recovery image contains two GUID matches, nvSCP.sys and nvsrootmbx.sys.
   A GUID occurrence alone does not identify which is a provider.
4. nvsrootmbx at RVA `0x30d00..0x30d70` creates and enables this exact GUID through
   KMDF table indices 77/78. The original slice runs in the emulator with fake
   WDF handles/callbacks. Success sets its local enabled flag; simulated create
   failure skips enable and leaves the flag clear.
5. Its DMF IOCTL table records at RVAs `0x1b090` and `0x1b0b0` describe
   `0x150214` (minimum input 32 bytes) and `0x150218` (minimum input 0 bytes).
   Both point to handler RVA `0x321e0`. The latter's branch comparisons match
   both IOCTLs. The table has zero minimum output sizes. These are framework
   minimums, not proof of complete payload validity.

Microsoft's primary headers identify
[KMDF indices 77/78](https://github.com/microsoft/Windows-Driver-Frameworks/blob/main/src/publicinc/wdf/kmdf/1.33/wdffuncenum.h)
and the
[DMF IOCTL record layout](https://github.com/microsoft/DMF/blob/master/Dmf/Modules.Library/Dmf_IoctlHandler.h).
The examined binaries contain the DMF IoctlHandler source-path/type strings.

## Pinned vendor inputs

These were extracted without running Windows executables from the existing
ASUS recovery `sources/wim/boot.wim`. Binaries remain in ignored sources/.

| File | Version | SHA-256 |
| --- | --- | --- |
| nvsrootmbx.sys | 2.10.0.0 | f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a |
| nvSCP.sys | 2.17.0.0 | d1cc1ae1a907d607ad979e84116a5febdb2f55136b612b210c68fdd57f901a47 |

INF matches are `ACPI\NVDA8600` for nvsrootmbx and `ACPI\NVDA8200` for nvSCP.
`inspect_auxiliary_drivers.py` checks hashes, binary versions, GUIDs, IOCTL
records and the actual provider-registration slice, including its failure path.
The nvSCP match remains a reference/consumer candidate; its role is not inferred
solely from containing the GUID.

Reproduce extraction using the bundled unpacker (not the vendor installer):

```sh
tools/usr/lib/7zip/7z x -y -osources/platform-drivers sources/wim/boot.wim \
  'Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/*' \
  'Windows/System32/DriverStore/FileRepository/nvscp.inf_arm64_d79e80462fadf1d5/*'
venv/bin/python scripts/inspect_auxiliary_drivers.py
```

## Linux target and remaining work

Live read-only sysfs exposes `NVDA8600:00` at `\_SB_.ROT0` and `NVDA8200:00`;
neither had a platform driver link during inspection. Candidate archived ACPI
ROT0 has two overlapping resources, `0x18010000/0x1000` and
`0x18010000/0x10`. This is the mailbox device, separate from the NPU's
`0x19000000` resource. Do not assume its two resources are independent windows.

No drop-in Linux SROOT implementation was identified in the local kernel source
or the initial upstream search. The NVIDIA NV-Kernels power_wrap/SSPM work is
not established as a SROOT transport implementation. A broad recursive upstream
tree query was truncated and cannot prove absence across that repository.

## Resource initialization and register spacing

Original resource-enumeration/mapping slice `0x30704..0x309d0` requires exactly
two translated resource descriptors. For memory descriptors (type 3), index 0
supplies context +0x38 physical base and +0x40 length; index 1 supplies +0x48
base and **+0x50 length**. It then calls `MmMapIoSpaceEx` (verified PE import
at `0x191d0`) with the **first** base/length and protection value `0x204`,
storing the mapping at +0x58. Later mailbox accesses use the **second resource's
length** as register stride. The second base is not independently mapped here.

`emulate_sroot_resources.py` executes this original slice with stubbed WDF
resource queries and `MmMapIoSpaceEx`. Two distinct resource configurations
verify base/length propagation, stride selection, mapping arguments and output.
A one-resource case skips mapping. These are synthetic OS resources, not an
observation of Windows hardware execution.

With archived ROT0 descriptors `0x18010000/0x1000` and
`0x18010000/0x10`, this implies **stride 16 bytes**, rather than two independent
MMIO apertures. The TX FIFO would be at logical offset 0x10, RX FIFO at 0x40,
and echo FIFO at 0x70, all inside the first 0x1000-byte resource. These offsets
remain conditional on live resource/table identity and the full ACPI power
path audit. Nothing has been physically mapped or read.

## Recovered TX primitive

Handler `0x321e0` dispatches firmware upload to `0x31c98` and reinitialization
to `0x31760`. The upload builder uses command byte `0x18` with three payload
words from device-context offsets `0x1c`, `0x70`, and the low word at `0x80`.
The latter is rejected if its high 32 bits are nonzero. DMA address allocation is traced below; ownership and release semantics
still need independent validation.

Original transport `0x5580` prepares the bus (`0x54c0`), sends/verifies an echo
(`0x5130`), then separately receives a firmware reply (`0x5328`).
`driver/n1x_sroot.c` implements only the TX/echo primitive, through callbacks.
The register spacing comes from descriptor +0x50, mapped base from +0x58.
The initialization path establishing these fields is verified below; live
resource identity still remains unverified.

| Offset relative to SROOT base | Observed TX operation |
| --- | --- |
| 2 × stride | Poll bit 0 until set before sending |
| 1 × stride | Write header, followed by payload words to the same FIFO |
| 3 × stride | Write 1 after the payload |
| 8 × stride | Poll bit 0 until the complemented echo is available |
| 7 × stride | Read complemented header and each payload word |
| 9 × stride | Write 1 to acknowledge the echo |

For zero-status/zero-metadata requests, the header is
`command | (word_count << 8)`. The Linux helper accepts 0–14 words. The
Windows encoder has additional fields; they are not assigned speculative
sequence/channel meanings. A header mismatch returns immediately; a payload
mismatch consumes the remaining echo words and acknowledges before failing.

`test_sroot_send.py` executes original ARM64 code at RVA `0x5130`, including its
header encoder, polling, writes and echo checker, in synthetic RAM. Twelve
successful traces match the Linux C component across counts 0/1/3/14 and
synthetic strides 4/8/16. Four echo-corruption cases check error behavior.
Every callback position in a three-word transfer is fault-injected; timeout
and invalid-input cases also pass. This does not emulate firmware or prove
physical register readiness.

Two deliberate Linux policies differ from Windows: polling is bounded and a
failed initial ready poll prevents all writes (Windows ignores that return).
On echo failure, Windows additionally runs `0x5018`, draining pending RX
status at 5 × stride via acknowledgments at 6 × stride, and echo status at
8 × stride via 9 × stride. Linux leaves this recovery to the transaction
owner rather than discarding a possibly significant firmware response.
After any attempted write, delivery must be treated as uncertain until
recovery/quiescence. Never automatically retry or free referenced DMA storage.
**A successful echo is not firmware-command completion.**

## Recovered reply primitive

`n1x_sroot_receive()` follows original receive routine `0x5328`: poll bit 0 at
5 × stride, read the header and indicated payload words at 4 × stride, then
write 1 at 6 × stride. It retains the raw header. Original decoder `0x4f70`
extracts command from bits 0–7, word count from 8–11, status from 12–23 and an
additional byte from 24–31. The additional byte is exposed as `detail` without
assigning a hardware meaning. Request encoding and reply decoding are not
symmetric.

Thirty-six synthetic reply traces and decoded field tuples match the original
ARM64 receive/decoder routines across three strides, four word counts, and
success/nonzero/all-set status fields. Every callback in a three-word reply is
fault-injected. The Linux output is published only after successful reads and
acknowledgment. Polling is bounded and checked before reading (the original
receive routine ignores that polling return). Count 15 is rejected before
payload reads/acknowledgment instead of exceeding the verified 14-word buffer;
recovery remains the caller's responsibility. Nonzero firmware status is
returned as data, not disguised as transport failure or command success.

These are separate primitives, not a complete boot transaction. The caller must
serialize preparation, TX, echo and RX, validate command/status/payload, and
retain DMA storage until an appropriate response or confirmed quiescence.

## Verified bus preparation

`n1x_sroot_prepare()` follows original `0x54c0` and its drain helper `0x5018`:
write `0xffffffff` at 12 × stride, write 3 at 3 × stride, drain pending RX
status (5/6 × stride), drain pending echo status (8/9 × stride), wait for bit 0
at 2 × stride, then read 11 × stride. Windows returns the low byte of the last
read; the Linux primitive maps a nonzero low byte to `-EBUSY`. Upper bits alone
do not cause failure. These are observed access patterns, not proof of reset,
interrupt-mask or other speculative register semantics.

The helper requires exclusive ownership of an **idle** mailbox. Draining
discards stale notifications; it must never be used to discard a response to
an outstanding request or to infer DMA quiescence. Each drain/readiness stage
has a bounded observation budget and errors propagate immediately. A final
pending observation returns timeout without another acknowledgment: a clear
observation must follow every acknowledged item before the helper succeeds.
Delays are Linux policy, absent from the original drain loop. Windows also
ignores the final readiness polling return; Linux checks it before proceeding.

`test_sroot_prepare.py` executes the original function, its original drain
helper and polling in an ARM64 RAM emulator. Forty-eight MMIO traces match
across three strides, pending-notification counts and status words, including
upper-byte-only status. Fourteen callback positions are fault-injected.
Stuck RX, stuck echo and missing TX readiness terminate within the Linux
budget. These tests do not establish physical device behavior.

## DMA common-buffer allocation

Original allocation slice `0x3b24..0x3da8` calls imported `IoGetDmaAdapter`
with description version 3. It stores the resulting adapter at context +0x60,
then calls the DMA_OPERATIONS function at +0x110. The ARM64 pointer-table layout
and argument signature identify this as
[AllocateCommonBufferWithBounds](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/nc-wdm-pallocate_common_buffer_with_bounds)
(see Microsoft's [DMA_OPERATIONS](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_dma_operations)).
The source bounds are 32-bit context fields +0x28/+0x2c, widened to 64-bit
minimum/maximum values; length comes from +0x70, flags and preferred node are
zero, and a cache-type output pointer is supplied. The origin of these bounds
and the returned cache type still need tracing.

| Context offset | Proven role |
| --- | --- |
| +0x60 | DMA adapter returned by IoGetDmaAdapter |
| +0x70 | Requested common-buffer length (u32) |
| +0x78 | CPU virtual address returned by common-buffer allocator |
| +0x80 | Logical device/DMA address written by allocator |
| +0x88 | Separate result of MmGetPhysicalAddress on CPU virtual address |

The allocation routine checks the **starting** logical address against both
bounds. The upload request subsequently uses the low 32 bits at +0x80 and
rejects a nonzero high word. Thus the device address must not be substituted
with the separate CPU-physical value at +0x88. Microsoft's API explicitly
requires use of the returned logical DMA address. A future Linux backend must
use the applicable DMA mapping API/domain; this evidence does not establish
that SROOT and DLA share an IOMMU domain or that identity mapping is safe.

`emulate_sroot_dma.py` runs the original slice with stubbed WDF/OS allocation
calls. It uses deliberately different logical/CPU-physical addresses, checks
stored values and all allocation arguments, tests both accepted start bounds,
rejects addresses immediately outside them, and exercises allocation failure.
The OS allocator owns the full-extent bound guarantee; the driver's redundant
start-address check alone does not establish it. No real memory was allocated
for DMA, and copy, flush/coherency, bounds origin and release remain unresolved.

## Remaining bring-up gates

Implement and verify full command completion policy, DMA lifetime,
and the complete firmware upload request. Verify the recovered resource/stride rule against live ACPI resources before
creating a physical backend. No mailbox MMIO,
IRP submission, firmware loading or reset was performed. Any eventual ROT0
binding requires live AML/power-path audit; the existing candidate DLA0 audit
does not cover an unexamined auxiliary device or establish live-table identity.
