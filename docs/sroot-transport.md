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

The primitives are composed by the exchange helper described below, but do not
constitute a complete boot transaction. The caller must
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
zero, and a cache-type output pointer is supplied. The bound origin is verified below; the returned cache type still needs tracing.

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
for DMA, and flush/coherency remain unresolved. Copy and observed release
behavior are traced below, without treating cleanup as proof of quiescence.

## Image framing, copying and observed lifetime

Original `0x314a0` obtains the input through KMDF indices 267
(`WdfRequestRetrieveInputMemory`) and 194 (`WdfMemoryGetBuffer`); these are
identified by Microsoft's [KMDF table](https://github.com/microsoft/Windows-Driver-Frameworks/blob/main/src/publicinc/wdf/kmdf/1.33/wdffuncenum.h).
It stores the CPU input pointer at +0x68 and full byte length at +0x70, rejects
length greater than context +0x30, then reads the first little-endian u32 into
+0x1c. Accepted identifiers are `0xc0300`, `0xc0302` through `0xc0305`, and
`0x90400`; `0xc0301` is not accepted. The DMF table imposes the outer 32-byte
minimum. The parser alone is not a complete length validator.

Copy routine `0x39b0` requires valid input/output pointers and size >4; it
copies **input +4, length -4** into the common buffer. It neither copies the
identifier nor initializes the final four allocated bytes. The original
upload builder `0x31f18..0x31fb4` still sends command `0x18` with words:
`[identifier, full_input_length, logical_dma_address_low32]`. The length is
not reduced by four. No cryptographic interpretation of the identifier or
opaque remainder is established.

`n1x_sroot_stage_image()` reproduces the framing/request words and payload
copy, but explicitly zeros the otherwise untouched four-byte tail. This is
**Linux policy**, not evidence of Windows allocator initialization or proven
firmware expectations. The tail's hardware significance remains untested.
The helper accepts at least 32 bytes, checks capacity, identifiers, the Windows window-derived length limit and the
entire DMA address extent against both supplied bounds before mutation, supports overlapping input
and output via memmove, and performs no allocation/submission. The caller supplies the DMA window and owns
DMA mapping, coherency and lifetime; the helper does not authenticate
an image. Logical DMA address zero is representable and not confused with a
null CPU pointer.

`emulate_sroot_image.py` executes the original validation, copy call site and
request-builder slice with stubbed KMDF/memcpy calls. A sentinel-filled
allocation proves the untouched tail; six accepted identifiers, four rejected
identifiers, size limits and three non-default sizes are checked.
`test_sroot_image.py` checks 36 Linux staging cases (including in-place copies),
invalid-input non-mutation, full-extent/address overflow and both actual P16
opaque variant sizes 67,508/77,492 bytes. These are byte/word contract tests,
not hardware boot or signature checks.

Original cleanup `0x3e00` calls DMA_OPERATIONS +0x18 (`FreeCommonBuffer`),
with adapter, full length, **logical** address, CPU pointer and cache-enabled
argument zero. A `dmb sy` precedes the call. It clears input/size/CPU/DMA/physical
fields but retains the adapter. Its guard skips freeing logical address zero;
that observed Windows guard is not a general rule that zero cannot be a valid
DMA mapping. The cleanup emulator checks arguments, fields and this edge case.

The original upload suffix `0x31fb8..0x32194` invokes cleanup on both transport
success and failure, then stops its timer and releases its lock. Four executed
cases confirm this, including nonzero firmware status/detail. The wrapper
requires both transport return and the **entire decoded status/detail word**
to be zero; otherwise it returns `0xc0000229`. This behavior does not establish
that all error returns imply DMA quiescence. Linux deliberately has no automatic
free/retry path yet. A barrier alone does not prove device completion, and the
copy path contains no independently established cache-maintenance operation.

## Windows DMA-adapter ownership and description

The original IOCTL dispatcher `0x321e0` calls `0x6ff8`, which obtains a DMF
module's buffer using `WdfMemoryGetBuffer` (KMDF slot 194 / +0x610) and loads
the WDF device handle at module-buffer +0x38. For upload it passes this handle
to `0x31c98`, together with the IRP/request handle. The allocation routine
`0x3b08` passes that device handle to `WdfDeviceWdmGetPhysicalDevice` (KMDF
slot 33 / +0x108), then passes the resulting PDO to `IoGetDmaAdapter`.
This is the provider's stored device/PDO path, not an observed reference to
the DLA device or its IOMMU domain.

`emulate_sroot_dma.py` now executes the complete dispatcher/module-buffer helper
up to the upload entry, captures the supplied handle, and reuses that handle
in the independently executed DMA allocator slice. Synthetic KMDF callbacks
return a distinct PDO, and the original code passes it unchanged to
IoGetDmaAdapter. These are handle-flow observations with fake OS objects, not
proof of a physical requester or stream ID.

The allocator's actual **64-byte DEVICE_DESCRIPTION** is decoded using
Microsoft's [published layout](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_device_description):

| Field | Observed value |
| --- | --- |
| Version | 3 |
| Master | 1 |
| ScatterGather / DemandMode / AutoInitialize | 0 / 0 / 0 |
| Dma32BitAddresses / IgnoreCount / Reserved1 / Dma64BitAddresses | 0 / 1 / 0 / 0 |
| InterfaceType | 17 (ACPIBus) |
| MaximumLength | Current full input length |
| DmaAddressWidth | 64 |
| BusNumber / DmaChannel / DmaPort / DmaControllerInstance / DmaRequestLine / DeviceAddress | 0 |

The flag order matters: byte 9 is **IgnoreCount**, not Dma64BitAddresses.
The latter legacy flag is byte 11. The nonzero version-3 DmaAddressWidth is a
separate field. Microsoft describes [ACPIBus](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_interface_type)
as an enumeration route, not a physical bus-master/SMMU identity. These fields
request a bus-master DMA adapter with a 64-bit address width; the **specific
common-buffer allocation** is independently bounded to the below-4-GiB registry
window. Neither flag nor width supplies a cache-coherency declaration or proof
that direct physical addressing is safe.

The earlier emulator captured 80 raw stack bytes for this description. It now
captures and checks only the actual 64-byte structure, avoiding interpretation
of neighboring stack storage. Live Windows DMA adapter internals and the
secure-side reader of command 0x18 still remain unknown. In particular, this
trace does not justify borrowing DLA0's Linux DMA device or forcing ROT0 coherent.

## Registry DMA window and upload timer

The pinned recovery INF (SHA-256
`ea6f08cd6a3df63f8423c11cbb5175ab794bc670c4bf5d7b6e825df5d30eaa40`)
installs `DmdAddrStart=0xe0000000` and `DmdAddrEnd=0xffffffff` as DWORD values
in the device hardware key. This is a **logical DMA window**, not proof of
reserved CPU-physical RAM or a safe physical mapping.

Original setup `0x30538..0x30704` builds these exact Unicode names and invokes
helper `0x2a88` twice. The helper opens the device hardware registry key,
queries four bytes per value, and closes each handle. Setup stores start/end
at context +0x28/+0x2c and **end minus start** at +0x30 (not plus one).
A missing start value or inverted bounds exits before resource mapping.
The verified INF therefore gives a maximum input length of `0x1fffffff`.
`emulate_sroot_policy.py` executes this initialization and registry helper
with synthetic KMDF registry calls, including missing/inverted cases.

Linux image staging now requires explicit inclusive minimum/maximum arguments,
checks the same end-minus-start length limit and requires the **whole**
allocation extent to fit that window. The P16 recipe must supply
`0xe0000000..0xffffffff`; earlier synthetic examples below that range do not
establish P16-compatible mapping. A 32-bit DMA mask alone cannot establish the
minimum-address constraint. The kernel backend still needs an appropriate
mapping/domain allocation strategy; the vendor registry values do not prove
how the live SMMU maps SROOT DMA.

The upload starts its WDF timer with the signed due-time constant
`-700000000` in 100-ns units, i.e. a **70-second relative deadline**.
Original timer callback `0x3fe0` retrieves the parent/context and sets byte
+0x18 to 1; predicate `0x3f80` returns that byte (or 1 for a null context).
The emulator executes callback/predicate before and after simulated dispatch,
then captures the actual WdfTimerStart arguments from the upload slice.
Original polling plus the original predicate also return 6 for an expired/not-ready
case and 0 for a ready register even when the expiration flag is already set.
The original readiness loop `0x4828` returns vendor code 6 if this predicate
fires before bit 0 becomes ready. Pending-notification drain `0x5018` does not
consult the flag, so the original overall operation is not guaranteed bounded
by that timer. Some callers also ignore the ready-poll return, as noted above.
Linux deliberately bounds each stage; no shared absolute deadline or actual
timer scheduling is implemented yet. This evidence does not make a
timeout a proof that firmware/DMA stopped.

## Complete mailbox exchange

`n1x_sroot_exchange()` composes preparation, TX/echo and RX into one callback-only
transaction. It validates all phase parameters before any preparation write,
returns immediately on an error and never retries, resets or frees referenced
DMA storage. The caller must exclusively own an initially idle mailbox, retain
any DMA mappings, implement a global deadline if needed, and validate the reply
before assigning command-completion semantics. Every individual stage has the
supplied bounded polling budget; this is not one shared 70-second deadline.

`test_sroot_exchange.py` executes the **complete original Windows wrapper at
0x5580**, including its original preparation, encoder, TX/echo, RX/decoder and
compiler stack-cookie helpers. Only the memory-mapped device is synthetic;
register operations are recorded against a simulated mailbox. Forty-eight
complete MMIO traces match across strides 4/16, TX/RX counts 0/1/3/14, zero and
nonzero status/detail, and initially pending stale RX notifications. Linux's
intentional drain delays are excluded when comparing MMIO trace order. Every
one of 21 callbacks in a three-word upload transaction is fault-injected; no
later stage runs after error, and no partial reply is published. Busy and
stuck-ready/echo/reply cases are also tested.

The original wrapper returns transport success even for a nonzero firmware
status (it exports the decoded status separately), and accepts a response
command byte differing from the request. Those behaviors are reproduced as
**transport semantics only**. Linux always returns the captured reply payload,
including on firmware-status errors; Windows skips copying the optional payload
when its low 12-bit status is nonzero. This preserves diagnostic evidence but
must not be interpreted as a successful firmware operation. The outer Windows
upload wrapper applies its additional status/detail check as described above.
No response-identity contract has yet been recovered from a real SROOT firmware
response, so the transport does not invent one.

## Separate reinitialization request

IOCTL `0x150218` reaches `NotifySrootReinitFw` at RVA `0x31760`. Its pure
normalizer `0x37f0` accepts either four bytes (little-endian image identifier,
implicit mode 1) or eight bytes (identifier followed by u32 mode). Identifiers
are the same six values as image upload; mode must be 1 or 2. Unsupported sizes,
identifiers and modes return `0xc000000d`. The surrounding WDF size gate rejects
sizes above eight; the helper's size checks also reject shorter non-four/eight
requests. These are request encodings, not established descriptions of what
modes 1/2 do in secure firmware.

Original builder `0x31a34..0x31a88` sends command **0x1a**, count **3**, payload
**[identifier, 0, mode]**. The zero middle word is part of the protocol; the
request does not reference an image buffer/DMA address. The known KMD subtype-1
input `02 03 0c 00 02 00 00 00` therefore becomes `[0xc0302, 0, 2]`.
`n1x_sroot_build_reinit()` implements this transformation, with no output mutation
on invalid input. Public command constants distinguish upload 0x18 and reinit
0x1a. The resulting three words use the existing exchange helper; transport
success alone does not prove successful reinitialization or NPU readiness.

`test_sroot_reinit.py` executes the original normalizer and request-builder
slice for 18 valid and 13 invalid cases, compares Linux payloads, tests null
parameters and matches a complete original/C exchange trace for the exact P16
request (TX header 0x31a). All WDF objects, readiness and replies are synthetic;
no real reset/reinitialization was attempted. Both wrappers require zero
transport and decoded status/detail on their success path.

**Do not impose an unconditional upload-then-reinit recipe from these two
IOCTLs alone.** Original KMD method-2 upload helper `0xd45cb0` invokes HAL +0x400
with subtype zero and records the successful variant; that function itself
does not append a subtype-1 call. The separately observed method-2 selection
slice `0xd47460` checks HAL byte +0x486: nonzero calls upload slot +0x408;
zero calls +0x400 with `(blob=NULL, length=0, subtype=1)`. Three original-slice
emulations cover byte values 0/1/255 and capture selected callbacks/arguments.
The enclosing function `0xd47430` continues into other core/start checks.
The initializer at `0x195d194` explicitly clears byte +0x486; configuration
`0xd44284..0xd442b8` reads registry value **RmSocDlaSrootReinitMode**. Only a
successful read of value 1 sets the byte to 1; other present values clear it,
and an absent value preserves it. `emulate_boot_policy.py` now executes the
initializer store and registry slice for absent/0/1/2/3/0xffffffff across both
HAL selectors, in addition to the existing method/remapper checks. The registry
boolean is distinct from the normalized reinit request's mode 1/2. Complete
cold/resume call ordering still needs tracing before defining Linux boot state
transitions. Do not infer this field from upload-success byte +0x48c, which is
a different field.

## Method-2 start ordering and write-free idle wait

The original HAL-1 initializer assigns +0x408 to upload helper `0xd45cb0`,
+0x400 to SROOT transport wrapper `0xd49ef0`, and +0x420 to orchestrator
`0xd47430`. A scan of direct BL targets finds calls to common loader `0xd439c0`
at RVAs `0xd402f4`, `0xd419b0`, `0xd42268`, `0xd4779c`; these references locate
further parent paths but do not alone prove live cold/resume invocation order.

`emulate_method2_start.py` now executes **the complete original 0xd47430**
function, not just its selection slice. With method 2 and optional feature
flags zero, its observed order is:

1. Upload (+0x408) if byte +0x486 is nonzero; otherwise reinit (+0x400, subtype 1).
2. If the request succeeds, wait via original `0xd4ce48` on the subobject at
   HAL +0x68. Its accessor slot +0x1d0 is the already decoded IDLESTATE 0x4c.
3. If the wait succeeds, invoke HAL +0xd8 with the u32 from subobject +0x50.
   HAL 1 assigns this to `0xd49800`; the lower register/accessor behavior of
   this final callback is separate from the orchestration experiment.

Twelve cases cover both selections, immediate/delayed idle, request error,
read error, persistent busy and a failure returned from the final callback.
Request failure prevents polling/final callback; idle failure prevents the
final callback. Windows ignores the final callback's return. Optional branches
controlled by +0x47d/+0x47e and other feature flags remain untested; no complete
boot-state machine is inferred from zeroed synthetic state.

**The method-2 path above does not call the direct CPUCTL start routine.**
Linux now exposes `n1x_dla_wait_idle()` separately from `n1x_dla_start_core()`.
The new helper performs only IDLESTATE reads/delays, requires no write callback,
and cannot implicitly select a core or write CPUCTL=1. The existing direct-load
start helper retains its CPUCTL write and delegates the polling loop to the new
helper. It must not be inserted into SROOT sequencing merely because its name
sounds like a generic boot step.

`test_core_control.py` compares the new write-free helper with original
0xd4ce48 for immediate/delayed idle and 5001-read busy timeout, plus callback
failures and invalid budgets. Existing selection/direct-start checks still
pass. The Linux policy continues to delay only between observations, propagates
read errors, and accepts idle on the final permitted observation; Windows's
delay/limit details differ as previously documented. Idle alone is not proof
of protocol readiness, inference or DMA quiescence. Physical resource mapping,
power/reset, post-wait configuration and full lifecycle order remain pending.

## Remaining bring-up gates

Implement and verify full command completion policy, DMA lifetime,
and the complete firmware upload request. Verify the recovered resource/stride rule against live ACPI resources before
creating a physical backend. No mailbox MMIO,
IRP submission, firmware loading or reset was performed. Any eventual ROT0
binding requires live AML/power-path audit; the existing candidate DLA0 audit
does not cover an unexamined auxiliary device or establish live-table identity.

The [ROT0 ACPI/DMA audit](acpi-audit.md) now identifies an additional prerequisite:
ROT0 lacks `_CCA` in both archived/candidate sets. With this kernel's required-CCA
configuration, the normal ACPI DMA path would install dma_dummy_ops if the live
namespace matches. An ordinary platform binding plus a 32-bit mask is therefore
not an established allocation strategy. Live IORT/coherency/requester evidence
is required before a physical DMA backend is implemented.
