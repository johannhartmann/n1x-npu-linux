# Verified contract and open boundaries

All offsets refer to the exact files below. Preferred image bases are
`0x140000000` for the KMD and `0x180000000` for the DLLs. RVA and raw file offset
are distinct. Analysis date: 2026-10-24.

## Target-selection correction (2026-10-25)

The original Windows hardware-ID classifier at RVA `0xd3b5c8` maps
`ACPI\VEN_NVDA&DEV_200A&REV_0034` to **0x254**, not 0x354. OS imports are
IoGetDeviceProperty(DevicePropertyHardwareID=1) and wcsstr, verified through the
PE import table. Six emulator cases cover REV_0034/2540 -> 254, REV_0036 -> 256,
unknown revisions and a failed property read. Constructor call sites
`0xd38b6c` and `0xd38c58` pass that result into `0x1950ec8`, then the recovered
classifier/HAL chain selects device profile 3 and DLA selector 1. Alternative
construction paths exist; a live Windows boot trace has not been collected.

HAL 1 firmware parser wrapper `0xd48410` passes the raw image at RVA `0x13ac1c0`
to the common parser `0xd4c640`. The image is 66816 bytes, SHA-256
`2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997`,
outer version 1, inner version 2, payload at 512 (66304 bytes), code at payload
0 (36864 bytes) and data at payload 36864 (29440 bytes). The original Windows
parser and Linux C parser agree on these ranges. The Linux parser now accepts
both observed inner versions 1 and 2, retaining the existing bounds checks.
The selector-1 descriptor getter at `0xd46760` additionally returns two
104-byte records with opaque boot variants; their selection and loading
remain separate unfinished work. They are not the raw image's section table.

**All firmware command/task/IL emulation sections referring to SHA-256
8f5b... are T35x/HAL-2 reference research. They do not prove HAL-1/P16 protocol
compatibility.** The P16's matching static resource chain is now explained,
and live ACPI identity is verified. Remapper selection and boot/security
handling remain unverified. Revalidate firmware-derived Linux behavior against
HAL 1 before using it on this machine. No NPU hardware access was performed.

## HAL-1 RV32 ping and IRQ response revalidation

The HAL-1 raw image uses **RV32**, including compressed JAL instructions and
32-bit saved pointers/return addresses, rather than the T35x image's RV64 code.
The correct RV32 disassembly resolves calls that RV64 misreads as ADDIW. All
HAL-1 execution evidence in this section uses Unicorn UC_MODE_RISCV32.

`emulate_profile1_commands.py` executes command dispatcher `0x102232`, ping
handler `0x10251c`, response wrapper `0x10044c` and mailbox handler `0x1021b2`.
Code maps at `0x100000`; initialized data at `0x180000`; a synthetic context is
installed through the global pointer at `0x1862bc`. Its ping scratch word is at
context +0x32ac. DMA functions `0x104ed8` and `0x105004` use RV32 register pairs
for u64 host addresses. Logging, command-entry/exit bookkeeping and the error
path's hang query are stubbed; DMA copies RAM. No actual device is accessed.

Eleven cases reproduce challenges 0/1/0x11111111/0x3fffffff multiplied by four,
overflow 0x40000000 (error 17), read/write errors (error 4), unsupported commands
0/2 (error 1), ping without response flags, and a missing host ACK. Basic success
response 0x102, overflow 0x110101 and DMA-error 0x40101 match the previously
observed T35x encoding. The firmware saves mailbox0/1, posts the reply, raises
SWGEN1 and waits for ACK 0xd1a0cafe, then restores both mailboxes. Missing ACK
remains waiting at the observation budget; no timeout is inferred.

`test_response.py` now couples the same Linux C capture/clear/ACK helper to
**both** images, for three challenges each including overflow. The actual HAL-1
firmware resumes after the C handler's ACK. IRQSTAT, interrupt routing and DMA
remain synthetic, so this validates the protocol sequence rather than a Linux
hardware IRQ implementation. Extended method flags/routing are not exercised.

This revalidates the basic ping/mailbox contract for HAL 1 only. It does not
establish core boot configuration, opaque boot variants, model/task ABI, address
translation, or instruction semantics for this image. Other sections explicitly
using the T35x hash remain reference findings until independently rechecked.

## HAL-1 task-header and UMD stride revalidation

The actual RV32 dispatcher routes command 7, after its engine-hang query, to
submit routine `0x104458` (A0 = shifted descriptor address; A1 = 1). Its pool
allocation scan and initial copy path read **60 bytes** via DMA function
`0x104ed8` into the first synthetic pool slot. The emulator executes this path,
including the original memory-clear routine, and stubs only DMA. Three addresses
(`0x20001000`, `0x1234567800`, `0xfffffffe00`) verify RV32 low/high DMA argument
reconstruction from the 32-bit method data shifted left eight bits. They do not
prove that these synthetic addresses are mapped or device-reachable.

`emulate_profile1_task_header.py` executes common-header checks starting at
`0x1046fa`. Header version at byte 8 must be 2 or 3; engine byte 9 must be
0x44; queue byte 20 must be zero (original validator `0x103f3e` is executed).
The next pointer at byte 0 is u64 and its low byte must be zero unless the whole
pointer is zero. All 256 values of version/engine/queue, five aligned and five
misaligned next pointers give 778 checks. Stops occur before enqueue/scheduling;
the descriptor's size, address-list/action fields and extension path are not
validated by this experiment.

A separate original UMD-version slice at `0x101766` reads byte `0x185d04` and
selects per-entry strides 36 for UMD version 1 and 64 for version 2; the other
254 byte values take its invalid-version path. The stack-stored stride and
branch endpoints are observed directly. UMD entry semantics remain unverified.

`test_task_descriptor.py` now feeds six actual Linux C serializer outputs
(two simple and four fenced descriptors) into **both** original common-header
checks. All are accepted. Terminal WRITE_SEM postactions are additionally
revalidated for HAL 1 in the following section. Other actions remain unverified.
This establishes the shared basic wire header, not complete task compatibility,
queue execution, a working task fence or numerical inference on the P16.

## HAL-1 WRITE_SEM and backing-allocation correction

Actual RV32 postaction interpreter `0x1037ac` uses opcode `0x80` for
WRITE_SEM: u64 host address, u32 value, then opcode zero terminates the stream.
Caller `0x103d56` reads postaction count at header +15 and list-table offset at
+18; its firmware-local context stores the host descriptor base at +128 as two
32-bit words. DMA helpers are the same HAL-1 low/high-address functions verified
for ping. A synthetic context is installed through global pointer `0x1862bc`.
Logging and DMA are stubbed; the original caller/interpreter execute in RV32.

**Unlike the T35x interpreter's 16-byte lookahead, HAL 1 fetches aligned
256-byte cache blocks.** A stream at descriptor offset 64 fetches the whole
block starting at descriptor offset zero. Thus the previous 96-byte fenced
allocation was insufficient for P16, although its wire fields were correct.
`N1X_DLA_FENCED_TASK_V2_BYTES` and the serializer now require/initialize **256
bytes**. The logical descriptor size field remains **96**, list record is at
60..63, stream at 64..77, and bytes 78..255 are zero padding. The descriptor
base must be 256-byte aligned, and every backing byte mapped. This fixes an
actual target-compatibility gap; it is not merely additional test coverage.

`emulate_profile1_postactions.py` executes 31 direct-interpreter cases: four
fence values across offsets 0/1/2/3/250/255, two malformed declared sizes,
DMA errors (including crossing a cache-block boundary), and two invalid opcodes.
Crossing streams fetch two consecutive 256-byte blocks before writing. The DMA
trace records source addresses and extents. The host word starts at a sentinel.
Malformed size 13 or 15 still writes the requested value, then returns -7;
read failures return -8 and write failures return -4. No physical cache ordering
or hardware ownership is inferred from RAM-copy DMA.

`test_task_descriptor.py` runs actual Linux C fenced outputs through the
original HAL-1 caller/interpreter. Four values produce DMA lengths [4, 256, 4]
(list record, cache block, fence write). Restricting the host allocation to the
old 96-byte extent causes the cache fetch to fail with -8 and leaves the fence
untouched. The emulator checks allocation extents explicitly, rather than
letting a broadly mapped synthetic RAM page hide that overread. Both malformed
list sizes are also checked through the caller and retain the write-then-error
behavior. The same 256-byte outputs still pass the T35x reference checks.

The caller immediately returns zero without any postaction/DMA operation when
its incoming task-status argument is nonzero (tested with 1 and -4). Therefore
an errored task may leave its fence unchanged, while a malformed list can write
a fence before reporting an error. The future host driver must combine task
error/status, deadlines and verified quiescence with fence observation. Fence
change alone does not prove success; absence of change alone does not prove
continued execution. Full scheduling, hardware interrupts and inference remain
unverified.

## HAL-1 address-list, DMA chunks and IL fetch extents

`emulate_profile1_address_list.py` executes cached-list loader `0x104a92` and
lookup `0x104b62`. With a synthetic engine -> task context it tests counts
1/9/31/32/33/511/512, u64 entries, valid boundary indices, index equal to count,
out-of-range indices, -1 and 0x10000, plus a DMA failure. The loader requests
**count × 8 bytes**, not the T35x round-up-to-256 request. Cached lookups read
firmware RAM without another DMA. Six actual Linux C address-list outputs also
round-trip through this HAL-1 loader/lookup; existing zero-padded allocation
sizes remain compatible with both variants. This is not a paged-list test.

The original lookup accepts `index == count` and truncates most supplied indices
to 16 bits: 0x10000 aliases entry zero. The experiment records the value returned
for index==count but does not interpret it as valid. Linux's recovered IL-slice
validator checks the original u32 index strictly against count before lookup;
explicit index==count and 0x10000 rejection tests cover these two firmware
weaknesses. Other model fields still need their own bounds validation.

To avoid equating a stubbed DMA call length with hardware transfer size,
`emulate_profile1_dma_chunks.py` additionally executes original read setup
`0x104e0a`, including channel setup and CSR save/restore helpers. Only the busy
query, logging and final hardware-issue primitive `0x10021c` are stubbed. Twenty
cases (ten lengths across two host addresses, including a low-word carry) record
contiguous power-of-two chunks from 4 to 256 bytes, with total length exactly
the requested length. Invalid length/source/destination four-byte alignment
fails before issuing chunks; each position in a 504-byte transfer is failed
independently. This supports exact **programmed** byte counts at the last
observed call boundary; no physical DMA, IOMMU reachability or coherency is
proven by a stubbed hardware issue function.

`emulate_profile1_il_reads.py` runs original IL dispatcher `0x102b68`. Its
RV32 ABI is engine pointer, host low/high address and instruction count.
DMA function `0x104ed8` and instruction executor `0x102abe` are RAM stubs;
only fetch extents and per-batch counts are observed. Ten cases
1/3/15/16/17/31/32/33/48/64 match the T35x fetch sequence exactly: one 256-byte
block through count 16; otherwise `count/16 + 1` blocks, including an additional
block for exact multiples above 16. All batch counts sum to the requested count.
`test_il_slice.py` compares these results to the existing C validator and
rejects one-byte-short allocations in all ten cases. The slice's field decoding
and opcode semantics remain separate from this extent proof.

## HAL-1 diagnostic network-prefix and INIT-IL execution

`emulate_profile1_init_il.py` uses the pinned HAL-1 firmware and unmodified
48 bytes of diagnostic INIT-IL from task0-ib offset 122880. Original table
initializer `0x102cec` installs 32-bit function pointers at `0x185d6c`.
The original batch interpreter is `0x102abe`; opcode 3 selects poll handler
`0x102a92`, requiring subtype byte 1. With synthetic ready-register values the
three actual instructions succeed, reading 32-bit values at internal offsets
0x7008, 0x5000, 0x6000. Handler invocation, offsets and success are observed,
not substituted with a stubbed instruction executor.

The HAL-1 handler adds register offsets to **firmware-internal base
0x01400000**, unlike the T35x emulator's 0x10019180000. Neither value by itself
identifies a Linux host physical mapping. The poll masks the read word, shifts
it by the instruction's byte-3 amount and loops **while equal** to its u32
comparison value. For these instructions, bit 0 set lets polling progress;
zero keeps the first instruction waiting at the observation limit. This limit
is not an observed firmware timeout. Wrong subtype, opcode zero and batch
count 17 return -3 before any register read. Batches are limited to 16.

The same emulator then executes actual address-list loader `0x104a92`, original
slice parser `0x102d7a`, instruction-fetch dispatcher `0x102b68`, batch and poll
handlers in one chain. It copies two synthetic mapped u64 address-list entries,
uses real diagnostic slice bytes `(index=1, offset=122880, count=3, trailing=16)`,
and observes DMA lengths [16, 256] (address list and instruction cache). The
u32 index/u32 byte-offset/u16 count layout is thereby revalidated for HAL 1;
this path does not interpret the final two slice bytes. The complete mapping
of all loadable memory IDs and non-init instructions remains provisional.

Finally it starts the **original v2 execution prefix at 0x100fea**, after its
previously verified address-list loading stage, stopping at `0x101052` before
subsequent network/task execution. The prefix resolves address-list[0], fetches
**256 bytes** into firmware network buffer `0x185740`, initializes the actual
handler table and calls the slice parser with **network +20** (`0x185754`).
The trace is [16, 256, 256] (address list, network descriptor, instruction cache),
followed by all three original poll handlers. This confirms that +20 is selected
by the real caller; it is not merely an emulator-chosen slice pointer.

The diagnostic task0-addr0 blob has only **128 initialized bytes**. The test
pads its mapped network allocation to 256 bytes; the explicit DMA extent guard
rejects the same prefix when allowed network backing is limited to 128 bytes.
A future model loader must provide accessible backing for the full 256-byte
read and determine how the runtime supplies any additional network fields.
Zero padding here permits examination of the **init prefix only**; it does not
prove that later fields or the whole network would execute correctly. No
production model allocator/executor is claimed implemented by this test.

All DMA is checked RAM copying, register ready bits are synthetic, and logging
is stubbed. No physical MMIO, SMMU mapping, firmware boot, queue scheduling or
numerical computation was performed. This closes the initial-loadable-to-handler
ABI gap while retaining the live-hardware and whole-model boundaries.

## HAL-1 word-upload and core-selection revalidation

`test_profile1_loader.py` executes both original HAL initializers and confirms
that +0x3e0 selects the same core-selection function `0xd48c40` for selectors 1
and 2. It sets BOOT_VECTOR_LO 0xb80 to 0x100000, writes BCR_CTRL 0xe68 = 0x10,
and checks readback bits 0x11. The guest firmware's RV32 versus RV64 instruction
set does not produce a different register sequence in this shared function.
That is evidence about this function, not proof of processor mode on hardware.

All 78 indexed accessor cases emitted by the original HAL initializers are
identical for selectors 1 and 2, including resource index zero and logical
offsets. Combined with the existing constructor/thunk decoder this revalidates
the word-transfer register mapping for HAL 1. It does not determine the physical
backend's active remapper setting or power state.

The original word uploader `0xd4bbe0` is now run with the HAL-1 image's actual
section sizes, rather than the former hardcoded T35x header. Its 16839 callback
writes match the Linux C uploader byte-for-byte, including DMACTL, IMEM/DMEM
control/data/block indices and ITFEN. Six injected callback failures propagate
with exactly the expected write prefix; two undersized code/data limits reject
before any write. The old T35x differential checks still run separately.
Readback verification remains stubbed, as previously documented.

This revalidates the existing callback-only uploader and shared selection
sequence. It does not justify bypassing the Windows auxiliary-driver boot path:
the selector-1 getter also exposes 104-byte variant records, and the orchestration
can select another loading branch. The actual P16 branch, power/reset,
remapper setting and successful boot still require independent evidence.

## Default boot policy and auxiliary-driver transport

`emulate_boot_policy.py` executes the original HAL initializers, default getter
and registry decision slices. HAL +0x410 points to `0x1c490`, which returns
**2**, for both selectors 1/2. Configuration at `0xd4419c` stores that value at
+0x460 unless RmSocDlaFwLoadMethod is supplied. Overrides 0/1/2 select those
values; other DWORD values normalize to 0. Twelve cases cover both selectors
and absent/0/1/2/3/0xffffffff overrides. Actual Windows registry state is unknown.

The same cases execute RmSocDlaRemapper handling: an absent value preserves the
previous +0x478 field, 0/2 store themselves, other values normalize to 1. The
contiguous constructor default-field sequence `0x195d154..0x195d1ac` is now
executed before these queries: it sets +0x478 to **1**. Thus an absent registry
override preserves mode 1. Three extra preservation cases still inject initial
values 0/2/0x77 to check the registry behavior independently. This establishes
the static default, not the actual installed Windows registry or live Linux
mapping. Earlier tests that supplied 0x77 did not establish that default.

The loader at `0xd439c0` uses method 1 for word upload, 0 for the ordinary DMA
branch, and 2 for HAL +0x408 (`0xd45cb0`) followed by an idle wait. Thus the raw
word/DMA image path is an alternate path, not the recovered HAL default.
`0xd45cb0` obtains a chip-ID-specific variant through +0x3e8 (`0xd46780`) and
submits it through +0x400 (`0xd49ef0`). Variant table records are 104 bytes and
contain two groups of three pointer/length pairs. The group is chosen by
(remapper != 1), while the routine tries variant indices 0..2. The exact
installed Windows flags and successful variant have not been observed.

`emulate_boot_transport.py` executes the original +0x400 wrapper and transport
`0xd3bbc8` until the imported IoBuildDeviceIoControlRequest boundary. Imported
KeInitializeEvent and IoBuildDeviceIoControlRequest identities, plus the later
IofCallDriver import, are checked from the PE import table. With a synthetic
auxiliary handle and outer transport mode +0x54=1:

- Normal subtype 0 requests IOCTL **0x150214**, passing the original image
  pointer/length unchanged, no output buffer and InternalDeviceIoControl=FALSE.
- Subtype 1 requests IOCTL **0x150218** with the eight bytes
  `02 03 0c 00 02 00 00 00`, also with no output buffer.
- A missing handle at outer device +0x308 returns 0xffff without reaching the
  OS boundary; a zero-length normal image returns 0x1f.

No IRP is constructed or submitted by the emulator. Target driver identity,
firmware framing/authentication, alternate outer-transport mode and a Linux
counterpart remain open. Earlier descriptions calling these blobs signed were
not proven: only their pointers, sizes, selection and transport are established.
Do not infer compression, signature format or ELF content from their prefix.
The provider has now been identified as nvsrootmbx.sys, bound by its INF to
NVDA8600 / ROT0. Its actual interface-registration slice and IOCTL records are
verified; see [SROOT transport](sroot-transport.md). Decoding its actual mailbox
protocol and establishing a Linux counterpart remain open, with the ACPI gate
extended to the auxiliary device before any binding.

## Identity

| Artifact | SHA-256 |
|---|---|
| NVIDIA 617.42 ARM64 installer | `425a75f54655ddcc8ee6859cec49bc431a17ef18094daaf59f205b159fb6e2bd` |
| nvlddmkm.sys | `73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987` |
| nvdla_runtime.dll | `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b` |
| T35x/HAL-2 research firmware | `8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75` |
| built-in diagnostic loadable | `51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543` |
| ASUS WinRE installer | `42c211bf3f14b226c5bfd04d2c6d8998997c84c90ef19f2db3839cae3d8fce1d` |

The ASUS INF binds `ACPI\VEN_NVDA&DEV_200A&REV_0034`, compatible
`ACPI\NVDA2042`, as NVIDIA NPU. Sections 003/004 use nvlddmkm. The INF class is
Display; D3DKMT use alone does not establish a particular MCDM driver model.
The matching hardware revision is not automatically the DLA IP version.

## Windows transport

The concrete DeviceKMI vtable is at nvcudla RVA `0xc2078`, confirmed by MSVC
RTTI. Slot `0xb0` points to RVA `0x9e70`, a D3DKMTEscape wrapper; slot `0xc0`
points to `0x9f78`, a D3DKMTSubmitCommand wrapper. The dynamic API resolver at
`0xb8b8` writes D3DKMTEscape at global VA `0x1800cfac8` and SubmitCommand at
`0x1800cfb30`.

The 32-byte escape envelope built at RVA `0x9ea4..0x9eb8` is:

| Offset | Size | Field |
|---|---:|---|
| 0x00 | 4 | adapter handle |
| 0x04 | 4 | device handle |
| 0x08 | 4 | escape type = 0 (driver private) |
| 0x0c | 4 | caller-supplied flags |
| 0x10 | 8 | private-data pointer |
| 0x18 | 4 | private-data size |
| 0x1c | 4 | context handle |

This is the Windows envelope, **not** the reconstructed private NPU protocol.
Runtime string tables contain nvdla_ping, nvdla_submit, nvdla_get_fw_ver,
queue allocation/release and buffer pinning names also found in the Orin UAPI.
Name matches do not prove payload or firmware ABI identity. Exact private blobs,
the selected runtime callbacks and their kernel consumers remain to be decoded.
Microsoft CFG moves some call targets to x15 before a guard call; a scan for
only adjacent LDR/BLR pairs misses those sites.

## Firmware

The descriptor at KMD RVA `0x108fd80` contains tag `0x354`, preferred pointer
`0x141480450`, and length `0x15500`. Getter RVA `0xd4a9a0` returns this descriptor;
consumer `0xd4a9c0` passes the same payload to parser `0xd4c640`.
The tag's full meaning is not established.

Payload raw file offset: `0x147dc50`. Size: 87,296 bytes. No validated embedded
RISC-V ELF was found; an ELF-looking byte sequence elsewhere failed header
validation. This is a raw 0x10fe container, not an ELF.

Outer six little-endian u32 words:

| Field | Value |
|---|---:|
| magic | 0x10fe |
| container version | 1 |
| total bytes | 0x15500 |
| ucode header offset | 0x100 |
| ucode payload offset | 0x200 |
| ucode bytes | 0x15300 |

The 19-word inner header matches the R38.4 `riscv_ucode_header`. Windows reads
monitor data offset/size at inner offsets `0x28/0x2c` and monitor code offset/size
at `0x30/0x34`, exactly as the public RISC-V implementation does. Values:

- Monitor code: offset 0, size 0xaf00 (44,800 bytes).
- Monitor data: offset 0xaf00, size 0xa400 (41,984 bytes).
- RISC-V startup instructions begin at container offset 0x200.
- Data includes firmware-t35x scheduler/RVV build-path strings dated November
  2025. This is not proof that the complete controller image requires RVV.

The original ARM64 parser was emulated in Unicorn: one valid case, seven
malformed-header cases and three null-argument cases pass expected status checks.
It returns the expected payload pointer and code/data offsets and lengths.
This validates extraction/header interpretation, not firmware boot or execution.
The independent C parser additionally checks actual buffer bounds, header version,
section overlap and alignment; synthetic mutation tests and the real image pass.

R38.4 provides RISC-V loading, AXI device, HFRP power, syncpoint emulation and
queue code. Its T25X firmware interface declares 3.0.0; a nearby Windows KMD
constant also says 3.0. Compatibility beyond these observations is not established.
The legacy Orin four-word Falcon OS header is not the 19-word RISC-V header.

## Windows firmware transfer (offline verified)

The load orchestrator at KMD RVA `0xd439c0` calls the parser through DLA
context slot `0x3d0`, then dispatches on `FwLoadMethod` at context offset
`0x460`: value 1 selects RVA `0xd4bbe0`, other non-2 values select
`0xd4bf80`. Value 2 has a separate path through `0xd4ce48`.
The selector is a Windows internal configuration, not a Linux setting to apply.

The original word-transfer routine at `0xd4bbe0` was executed in Unicorn with
only RAM-backed abstract callbacks. Its complete 22,039-call trace matches the
extracted firmware: 11,200 code words followed by 10,496 data words, with block
indices updated every 64 words (256 bytes). Code uses callback slots
`0x40/0x60/0x50`; data uses `0x70/0x90/0x80`. These slots belong to the
subobject passed into the routine, not the parent DLA HAL table. Error injection
at each of eight callback slots propagates status `0x65` and prevents the next
stage. This establishes the copy ordering and error handling without hardware.

The shared next function at `0xd4cad8` is **readback verification**, not CPU
startup: it allocates buffers, reads code/data through callbacks `0x48/0x78`,
and compares the results to the input. This function is stubbed in the transfer
emulator. The alternate transfer routine at `0xd4bf80` builds an aligned DMA
buffer, shifts its bus address by 8, issues commands in 256-byte steps and polls
completion. An offline emulation now verifies its allocator/copy operations, 175 code
commands `0x2610`, 164 data commands `0x2600`, correct 256-byte alignment for an
unaligned synthetic IOVA, nine callback error paths with cleanup, and a busy
poll timeout after 5,001 reads. These are synthetic callback responses, not a
DMA execution or proof of the hardware timing.

The original HAL initializer at `0x1951238` has also been emulated for selectors
0, 1 and 2. Selector 2 installs the verified firmware parser/getter. Its mapping
to the live hardware and the subobject's concrete register callbacks remain open.
The callback at `0xd4aa30` maps `0x1f800000` and `0x1f900000` and toggles fields;
these addresses are outside the historical DLA `_CRS`. Its role is unresolved,
and it is not a verified NPU boot sequence. No such writes have been issued.

## Register mapping and core control

Constructor RVA `0x195d114..0x195d134` installs descriptor `0x108e9a0` and
vtable `0x108e9b8` into the subobject at parent+8. Its forwarding thunks subtract
8 from the context argument and dispatch via fields relative to the subobject.
Accounting for this adjustment connects the transfer callbacks to the HAL table.
`decode_transfer_registers.py` verifies constructor/thunk bytes and joins them to
original accessor emulation. This establishes 23 transfer/core-control/method slots.

| Subobject slot | Operation | Logical register offset | Role |
|---|---|---|---|
| 0x40 / 0x50 / 0x60 | write | 0x180 / 0x184 / 0x188 | code control / data / block index |
| 0x70 / 0x80 / 0x90 | write | 0x1c0 / 0x1c4 / 0x210 | data control / data / block index |
| 0xf8 | write | 0x48 | ITFEN |
| 0x100 / 0x108 | read / write | 0x444 | TRANSCFG |
| 0x118 | write | 0x10c | DMACTL |
| 0x128 | write | 0x110 | DMATRFBASE |
| 0x138 | write | 0x114 | DMATRFMOFFS |
| 0x148 | write | 0x11c | DMATRFFBOFFS |
| 0x150 / 0x158 | read / write | 0x118 | DMATRFCMD |
| 0x160 / 0x168 | read / write | 0xb88 | CPUCTL |
| 0x1d0 | read | 0x4c | IDLESTATE |
| 0x190 / 0x198 | read / write | 0x68 | MTHDID |
| 0x1a8 | write | 0x6c | MTHDWDAT |

R38.4's non-firewall definitions agree where present. The block-index role at
0x210 is observed in Windows; that public RISC-V header does not define it.
The C uploader implements the observed word-upload sequence, checks the complete
container and caller-provided code/data limits before writing, and stops on the
first callback error. Its full 22,039-write trace matches the original ARM64
routine; ten injected failures and five preflight failures pass. It does not
include readback verification, power/reset, CPU selection/start or an MMIO backend.

Original core selection at RVA `0xd48c40` writes logical offset `0xb80` with
`0x100000`, then `0xe68` with `0x10`, and reads back `0xe68` checking mask `0x11`.
Original start at `0xd49e90` writes `1` to `0xb88`, then calls `0xd4ce48` to wait
for IDLESTATE to become zero. Five offline cases cover immediate idle, delayed
idle, 5,001-read timeout and read/write errors. The Windows start wrapper ignores
a failed start write; a Linux implementation must propagate this error instead.
Idle is not proof of a firmware ping or inference readiness. Another CPUCTL
wrapper (`0xd48ad0`) writes 4/8; its role must not be confused with the start write.

Selector initialization at RVA `0xd3c8d0` maps input `0x303033` to selector 1,
`0x404044` to selector 2, and zero to selector 3. The source of this input on the
live P16 still needs tracing; ACPI revision 0x34 alone does not prove selector 2.

**Logical offsets are not physical addresses.** The resource reader at
`0xd39878` resolves a resource descriptor before accessing registers.
Translator `0xd392c0` optionally maps a logical offset using 12-byte entries
`{u32 logical_start, u32 mapped_start, u32 length}` from resource+0x18, with the
entry count at +0x20. Without a table it passes offsets through. Some accessors
emit `0x80400330`; adding that to `_CRS` resource 0 would be unjustified. The
live descriptor population and mapping to both ACPI apertures remain
required before attaching any physical MMIO backend. The concrete static table
installer and contents are recovered below.

## Recovered remapper tables

The setter at RVA `0xd39ae8` stores a table pointer/count in the selected
resource descriptor. NPU installer `0xd49e20` selects two concrete static tables
using DLA context+0x478, configured via `RmSocDlaRemapper`:

- Value 1: table RVA `0x14803d0`, count 10 from RVA `0x1480338`.
- Value 2: table RVA `0x1480340`, count 11 from RVA `0x1480334`.
- Other installer values return `0xffff`; this must not be confused with the
  translator's independent no-table pass-through behavior.

Mode 2 maps logical 0..0x1ff to 0x2000..0x21ff, 0x210..0x21f to
0x2210..0x221f, 0x444 to 0x2244, 0xb80..0xb8f to 0x2280..0x228f, and
0xe60..0xe7f to 0x2380..0x239f, with additional bounded ranges preserved in
`driver/n1x_dla_registers.c`. These agree with R38.4's firewall offsets for the
transfer and core-control registers. Mode 1 exposes a smaller subset that does
not include the firmware upload ports. Neither table accepts 0x80400330.

`emulate_address_translation.py` executes the original installer, setter and
translator in RAM and tests 6,303 cases, including each range's edges, holes,
unaligned offsets and pass-through behavior. The Linux C translator matches
both mapped modes and additionally rejects unaligned direct access, insufficient
resource space for a complete u32, invalid modes and null output. Failures leave
output unchanged. Kernel-object compilation succeeds.

This closes the static table contents and translation algorithm. It does **not**
establish the live mode or identify the Windows resource descriptor with a P16
physical ACPI aperture. The firmware-upload callback receives logical offsets;
a future MMIO backend must apply the independently established map and resource
bounds before any access.

## Windows hardware-profile chain

`inspect_windows_profiles.py` executes the original ID classifier at RVA
`0xd3c0e8`, device HAL initializer `0x194fb08`, ID getters, resource-list
providers, and constructor branch `0xd39508..0xd395bc` in isolated RAM.
IDs 0x254/0x256 map to device profiles 3/4, DLA input 0x303033 and selector 1.
ID 0x354 maps to profile 6, DLA input 0x404044 and selector 2. This establishes
the internal link from ID 0x354 to the extracted firmware descriptor tag 0x354;
the separately recovered hardware-ID classifier now maps REV_0034 to 0x254; see the correction above.

The profile-6 provider at `0xd42380` returns three 24-byte static records from
RVA `0x13ac170`: id0 physical 0x19340000/0x40000, id1 0x3a340000/0x40000,
id2 0x31380000/0x10000. Profiles 3/4 use provider `0xd3fcc0` and two records at
`0x13ac140`: id0 0x19000000/0x40000, id2 0x31380000/0x10000.
The generic resource mapper `0xd3fd80` preserves these IDs and maps the supplied
base/length into device resource descriptors. Reported physical addresses were
never mapped in the emulator.

The P16's candidate ACPI `_CRS` matches the **older** list, not profile 6.
This discrepancy is a required bring-up investigation, not grounds to infer a
working physical address. See [the ACPI audit](acpi-audit.md) for all six table
hashes, offline power-path review, and remaining live-table verification.

## Linux core control and method transport

`driver/n1x_dla_core.c` implements selection/start through the common logical
I/O callbacks in `n1x_dla_io.h`. The selection trace matches original Windows
RVA 0xd48c40 after an additional R38.4-based check that BOOT_VECTOR_HI (0xb84)
is zero. It propagates every I/O error and rejects invalid BCR readback; Windows
can log/ignore such errors. No power/reset sequence or MMIO backend is supplied.

Start writes CPUCTL=1 and polls IDLESTATE=0 with a caller-selected microsecond
interval and at most 5,001 reads. Linux delays only between polls and accepts
idle on the final read. The interval unit is a Linux API contract, not a claim
about the original Windows delay helper. Tests compare original start writes
and read sequences, then exercise high-vector rejection, failed selection,
I/O errors, timeout and invalid polling policy.

The original method sender at RVA 0xd4c800 reads MTHDID (0x68) until pending bit
16 clears, writes MTHDWDAT (0x6c) first, then writes MTHDID. The reconstructed
subobject/HAL chain establishes all three offsets. Linux implementation
`driver/n1x_dla_command.c` matches successful and injected-error traces. Its
busy timeout is bounded to 5,001 total reads; the Windows routine performs an
initial read plus up to 5,001 retries. The caller must serialize commands and
establish DMA visibility before submission. Submission is not completion, and
a failed final ID write does not prove that the command was not received.
Referenced DMA buffers must survive until completion or verified quiescence.

Windows ping at RVA 0xd48110 allocates 4 KiB of DMA memory, writes challenge
0x11111111, passes its bus address shifted right by 8 with command 1, and waits
for the word to become 0x44444444. This matches R38.4 `nvdla_ioctl_ping`'s
command-1/aligned-address/challenge-times-four contract. It is evidence for a
shared ping ABI, not proof of the full task descriptor ABI or a successful
ping on the P16. DMA allocation, visibility, completion and actual hardware
ping remain unimplemented in the Linux components.

All seven Linux C components compile as kernel objects. The full word-upload
differential check continues to pass with the shared I/O callback layout.
No loadable module, autoload alias, hardware binding or device-access backend
has been introduced.

## Task descriptor checks from the RISC-V firmware

Radare2 disassembly of the extracted monitor code exposes separate descriptor
checks, found through PC-relative references to the firmware's diagnostic strings.
`riscv_string_refs.py` finds candidate AUIPC/ADDI references; manual disassembly
establishes the sites used below. RISC-V execution in Unicorn uses isolated RAM,
with no firmware initialization, DMA engine or MMIO mapping.

`emulate_task_header_checks.py` runs 1,033 cases against the original firmware:

- The common task header is copied with length 60 at code VA 0x104cc4..0x104cd0.
- At 0x104d20, byte offset 8 must be version 2 or 3.
- At 0x104b04, byte offset 9 must equal engine ID 0x44.
- At 0x104b2a, byte offset 20 is passed to 0x1045d6, which accepts only queue 0.
- At 0x104b52, the next pointer at offset 0 must be zero or 256-byte aligned.
- A **different UMD descriptor** check at 0x1018e4 accepts version 1 (stride
  36 bytes) or version 2 (stride 64 bytes). This is neither the common kernel
  descriptor version nor the FlatBuffer container version.

The builtin diagnostic's blob `task0-addr0` begins with LE u32 value 1, consistent
with the first supported UMD version. This observation does not decode the full
UMD format or prove that the provisional FlatBuffer projection is complete.

`driver/n1x_dla_task.c` builds a simple common version-2 descriptor for queue 0,
with no actions, flags or v3 extensions. It encodes the public R38.4 packed
`dla_task_descriptor` layout in little endian without relying on host C padding.
An independent test compiles the actual public header and compares every byte;
the produced descriptors also pass the firmware common-header checks. Bad
arguments leave output untouched. The builder does not validate DMA ownership,
reachability, address-list contents or timeout units. The checks deliberately
stop before scheduling; full address/action/descriptor compatibility and a
numerical task still require validation.

## Executed firmware dispatcher, ping and response handshake

`emulate_firmware_commands.py` runs the **original RISC-V** dispatcher at
0x102382, ping handler 0x102576 and response helper 0x102326. Logging and the
engine-hang query are stubbed; DMA helpers perform RAM copies; the register
page is RAM with a simulated host ACK. The firmware itself multiplies ping
challenges by four, constructs response words, triggers the simulated interrupt
and restores the old mailbox values after ACK.

Twelve cases cover four valid challenges, u32 multiplication overflow, each DMA
failure, unsupported commands, disabled response flags and missing host ACK.
Captured responses match the public message/command/error layout: low byte
message, next byte command, next byte error. Command 1 success yields 0x102;
DMA error yields 0x40101; challenge overflow yields 0x110101. Error 17 is **not**
in the R38.4 error enum, so that enum cannot be treated as exhaustive here.
Mailbox1 echoes the command data (the aligned DMA address for ping).

The response helper posts the response at internal firmware register addresses
0x01400040/44, enables/raises SWGEN1, and loops until mailbox0 equals
`0xD1A0CAFE`. It then restores the previous mailbox pair. With no host ACK the
bounded offline observation remains in that loop; it is not a firmware timeout.
Internal RISC-V register addresses are not the P16 physical CPU apertures.

The Linux helper `n1x_dla_take_response` reads pending SWGEN1 from logical
IRQSTAT 0x8, captures mailbox0/1, writes 0x80 to IRQMCLR 0xd24 and IRQSCLR 0x4,
then writes the ACK to mailbox0. Clear/ACK ordering follows the public R38.4 ISR.
The added pending check avoids acknowledging an unrelated/spurious interrupt.
Failures leave the caller's output unchanged; partial clear/ACK failure requires
recovery, not an assumption that DMA storage can be freed.

`test_response.py` couples this C helper to the actual firmware emulator for
successful and overflow pings. The real firmware resumes and restores mailboxes
after the C helper's ACK. Six I/O failure prefixes and a spurious interrupt are
also checked. IRQSTAT/routing are synthetic, not tested hardware behavior.

The firmware switch table at code VA 0x10ad88 has active paths for commands
1, 6, 7, 10, 11, 12–17 and 25. Entries 2–5, 8–9 and 18–24 go to the invalid-method
path. The purpose of command 25 is not established. In particular the public
header's LPWR commands 23/24 cannot be assumed usable in this firmware.

**Command completion is not task completion.** Command 7 calls the queue-submit
handler before returning through the common command response path. A driver
must keep task/model/DMA resources alive until an actual task fence/status or
verified device quiescence; receipt of message 2 for command 7 is insufficient.
Interrupt handling must be working before firmware can wait on a host ACK.
No physical IRQ, MMIO, DMA, firmware boot or inference has been tested.

## Setup command ABI differences

`emulate_firmware_setup.py` runs 18 cases through the original dispatcher,
response/ACK path and setup handlers. DMA is a RAM copy and logging is stubbed;
no engine register backend or device is used.

- Command 10 reads exactly 20 bytes: u32 subcommand, u64 data, u64 reserved.
  Subcommands 1 and 2 copy only the low 32 bits of data to firmware variables
  at `0x18b200` and `0x18b204`. Subcommands 0 and 3 return error 3. Field names
  TRACE_ENABLE and TRACE_EVENT_MASK come from the public header; their downstream
  effects have not been exercised. Read failure propagates error 4.
- Command 11 reads a four-byte region ID, then dispatches IDs 1, 2, 3 and 6.
  IDs 0, 4, 5 and 7 return error 11. This differs from the public enum, which
  names region 4 GCOV and does not name region 6. Do not rename 6 GCOV by analogy.
  Region 1 handler `0x101df8` copies a 16-byte record to `0x1889b0`: u32 ID,
  u64 address at offset 4, u32 size at offset 12, matching packed region_printf.
  Region 2 reads 56 bytes, matching the public GoS record's size. Regions 3 and
  6 read 16 bytes and may perform additional reads when address/size are nonzero;
  their complete buffer semantics remain open. Zero address/size succeeds for
  all four IDs, but is not evidence that logging is configured or safely disabled.
- **Command 17 is a preemption path, not SET_INIT_CONFIG.** The dispatch calls
  `0x105e78`, whose original diagnostic says the current state does not allow
  preemption. With actual firmware state variable `0x18930c` set to 2, 3 or 4,
  it returns error 12 without DMA. Static analysis shows the allowed path calls
  `0x105d5c` and executes preemption-related engine logic; it has not been run.
  The R38.4 public enum assigning SET_INIT_CONFIG=17 cannot be reused here.

These observations invalidate a blanket reuse of Jetson command numbers even
where the task and semaphore layouts agree. No initialization command list has
yet been established for the P16. Firmware-local BSS variables extend beyond
the initialized data blob; the setup emulator maps zero-filled RAM through
`0x190000` to cover the observed `0x18b2xx` globals. This is an emulator memory
model, not evidence for a physical host aperture or firmware RAM capacity.

## Windows region construction and Linux serialization

The original selector-2 HAL has +0x138 pointing to the ping routine `0xd48110`,
+0x160 to the region builder `0xd49c20`, and +0xf8 to the method wrapper
`0xd49fc0`. The loader `0xd439c0` calls ping after the upload/start path,
then conditionally calls +0x160 with IDs 3, 1, 5 and 7. The controlling fields
are +0x468, +0x46c, +0x480/+0x47e, and +0x494 respectively. Their actual P16
installed overrides have not been established. The original constructor now
confirms defaults enabling the region-5 path: +0x480=1 and +0x47e=1. These are
constructor/default-code observations, not a live Windows boot trace. The
region-5/7 rejection below concerns the **HAL-2** image only. HAL-1/P16 region 5
has since been independently revalidated; see the section below.

`emulate_windows_region.py` executes the original ARM64 builder, with a synthetic
buffer-provider callback (+0x158), CPU/DMA address pair, copy/cache/OS helpers,
and method callback. It proves that Windows writes a packed 16-byte record:
u32 region at 0, u64 (buffer IOVA + provider offset) at 4, u32 region size at 12.
It sends command 11 with data = **base buffer IOVA >> 8**, not the registered
region address >> 8. The descriptor and registered storage share one provider
allocation in these Windows test cases; this does not require Linux to share it.

The resulting records for IDs 1/3/5/7 are passed through the original RISC-V
dispatcher and response/ACK code. IDs 1 and 3 succeed; IDs 5 and 7 return error
11. The trace case uses a zeroed eight-byte ring header at the referenced region
and observes the firmware reading it. No ring output or wraparound is tested.
A provider failure sends no command and writes no record; a method-send failure
propagates its status. Synthetic cache maintenance cannot prove coherency.

The Linux `n1x_dla_build_region` serializer supports the verified 1/3 record
format and the separately revalidated HAL-1 region-5 record. Four byte-for-byte comparisons use different provider offsets/sizes
against original Windows execution. Seven preflight cases cover invalid output,
short storage, unknown ID, empty/unaligned address and zero/wrapping size, without
output mutation. These rejection rules are Linux API policy, not claimed firmware
validation. The caller still must own/map both allocations, initialize valid
region contents, encode a representable 256-aligned command-record address,
maintain DMA visibility/lifetime, and handle actual firmware errors. This helper
does not allocate buffers, initialize trace-ring state or enable logging.

## Task postaction completion evidence

`emulate_postactions.py` executes the original RISC-V postaction interpreter
at code VA `0x103cd2`, with logging stubbed and DMA replaced by RAM copies.
The firmware accepts a 14-byte stream: opcode `0x80`, little-endian u64 host
address, little-endian u32 value, then opcode `0x00` (terminate). This matches
the public WRITE_SEM payload. Sixteen cases cover all four stream alignments
and four u32 values, including zero and `0xffffffff`.

The opcode fetch performs a 16-byte aligned DMA read. For the valid terminal
WRITE_SEM stream the firmware reads the terminating opcode before writing the
four-byte semaphore. Both reads require accessible backing beyond the 14-byte
stream: for an aligned stream the second read starts at byte 12 and ends at
byte 27. The wire size must remain 14; allocation padding is separate.

**Malformed lists can still write the semaphore.** Declaring 13 or 15 bytes
instead of 14 causes the firmware to write the value, then fetch/check the
terminator and return `-7`. Thus a fence value alone is not evidence of a
successful task. The driver must validate descriptor/action lengths and retain
an independent task-error/status path. Injected failures in the first/second
DMA read return `-8`; failure of the semaphore write returns `-4`. These three
simulated failures leave the host sentinel unchanged. Unknown opcodes 1 and
255 return `-7` without a write.

The 23 standalone cases exercise the interpreter directly. In addition,
`test_task_descriptor.py` now runs the original caller at `0x1043b0` with the
actual Linux C serializer output. It copies the 60-byte header into a simulated
firmware task context and sets the context's host-descriptor pointer at +128.
The original caller reads num_postactions at header +15 and postactions at +18,
DMA-reads the four-byte list record, then invokes the real interpreter with
host base + list.offset and list.size. Logging and DMA remain the only stubs.
This establishes single-list traversal, not task execution or the correctness
of the preceding queue/context-construction path.

`n1x_dla_build_fenced_task_v2` now emits 256 initialized bytes to also cover
the P16 HAL-1 cache fetch described above: header 0..59, list record 60..63,
WRITE_SEM stream 64..77, padding 78..255. The logical size field is 96 and the
list size is 14. This T35x caller's observed lookahead reads 76..91 are a subset
of that backing allocation; its former 96-byte allocation was not valid for HAL 1. Four fence values are compared against
compiled public structures and passed through both common-header checks and
the actual caller/interpreter. The original basic serializer remains available.
The fence address must be nonzero and four-byte aligned; the caller must use a
mapped coherent word, initialize it to a distinct value, and keep buffers alive
until verified completion/quiescence. No CPU barrier is implied by serialization.
Neither physical DMA ordering/cache coherency nor task completion on actual
hardware has been demonstrated.

## Address-list DMA and lookup

The v2 execution path at `0x101c18` passes the reconstructed address-list pointer
and u16 count to `0x101072`. Its loader `0x104ed6` caches up to 512 u64 entries
in firmware RAM at `0x187700`. The DMA length is round_up(count * 8, 256), not
just count * 8. For larger counts a separate paged lookup path exists; it has
not yet been exercised. The diagnostic model's nine entries use the cached path.

`emulate_address_list.py` executes the original loader and lookup `0x104fc0`
with a simulated engine/task context. Only DMA and logging are stubbed. Seven
sizes (1, 9, 31, 32, 33, 511, 512) verify padded bulk reads and first/last-index
values. The debug firmware additionally DMA-reads each looked-up u64 from host
memory to compare against its cache. Bulk-read failure propagates error 4.

**Lookup accepts index == count.** In all seven RAM cases it returns the zero
from backing storage after the valid entries, rather than rejecting it. Indices
count+1 and -1 return error 3 without modifying the output word. This is an
observed boundary behavior, not permission to use that index. Linux-side model
validation must require every index to be strictly less than count before
submission. That model validation has not been implemented yet.

The DMA helper `0x1054da` checks source, destination and transfer length for
256-byte alignment before its hardware-transfer loop (`0x1054e2..0x105502`).
The task serializer therefore now rejects address-list IOVAs aligned to only
8 bytes; previous validation was insufficient. The emulator replaces this DMA
helper with a RAM copy, so its round trips alone do not prove that alignment
requirement or physical DMA behavior; the check is visible in the original code.

`n1x_dla_address_list_bytes` and `n1x_dla_build_address_list` support the verified
cached path, zero-pad the allocation, and serialize full little-endian u64 values.
Six C-generated allocations run through the original firmware reader/lookup;
five preflight errors leave output unchanged. Zero addresses are preserved,
not certified valid. Source/output must not overlap. Reachability, allocation
ownership, model index/offset bounds and coherency remain caller obligations.

## Network instruction-list slice bounds

The original v2 execution path resolves address-list entry 0 and DMA-reads
**256 bytes** into its network configuration at `0x188740` (`0x1011d8`). The
diagnostic blob named task0-addr0 contains only 128 initialized bytes; any future
host allocation must provide the full accessible read extent. Its contents are
not thereby proven a fully compatible network descriptor.

The INIT_IL call at `0x101218` passes network +20 to `0x103212`. That helper reads
u32 address-list index at +0, u32 byte offset at +4, and u16 instruction count at
+8; it resolves the index, checks u64 address addition, and calls `0x102fde`.
The last two bytes of this 12-byte slice are not read by that helper. In the
diagnostic's observed field the tuple is (index=1, offset=122880, count=3,
trailing_u16=16). Under the provisional memory projection index 1 is task0-ib,
124928 bytes; both its 48 logical instruction bytes and its 256-byte fetch fit.
This observation does not decode the instruction semantics or validate the model.

`emulate_il_reads.py` executes the original `0x102fde` dispatcher, stubbing only
logging, 256-byte DMA fetches and the instruction executor `0x102d60`. Ten counts
verify actual fetch offsets and dispatched batch counts. Instructions are 16
bytes. Counts <=16 fetch one 256-byte block. Counts >16 fetch
`(count / 16 + 1) * 256` bytes with integer division. In particular count 32
fetches 768 bytes and dispatches batches 16,16,0; simple round_up(count*16,256)
would underestimate the read extent. This is firmware behavior, not a reason
to submit an empty instruction list.

`n1x_dla_validate_il_slice` checks a single recovered slice against caller-owned
buffer ranges, requires index < number_of_buffers and a nonzero count, checks
256-byte source alignment and address-addition overflow, and bounds the full
DMA extent rather than just the logical instruction bytes. It leaves output
unchanged on error. Its ten computed extents match original dispatch execution;
ten one-byte-short ranges and six malformed slices are rejected. The actual
diagnostic INIT_IL field passes with a synthetic mapped address. This is one
building block: complete network schema traversal, instruction opcode validation,
embedded addresses/register references and all other model slices remain open.
The helper is not a complete submission validator and is not wired to hardware.

## Diagnostic INIT_IL instruction semantics

`emulate_init_il.py` runs the original opcode-table initializer `0x10316e`,
batch dispatcher `0x102d60`, and poll handler `0x102d2a`. Only logging is stubbed;
engine registers are synthetic RAM. The actual three 16-byte instructions are
extracted from task0-ib at offset 122880, without modifying their bytes.

All three have opcode 3 at byte 0, subtype 1 at byte 1, shift 0 at byte 3,
u32 register offset at +4, u32 mask 1 at +8, and u32 comparison value 0 at +12.
Handler `0x102d2a` checks subtype == 1, then repeatedly reads the register and
loops while `(register & mask) >> shift` equals the comparison value. Thus these
instructions wait for bit 0 to become nonzero at offsets `0x7008`, `0x5000`,
and `0x6000`, in that order. They are **polls, not register writes**.

With synthetic ready bits set, all three complete and return zero. With the
first bit zero the 1000-instruction observation remains in the first poll; no
second handler is reached. This is an observation limit, not a firmware timeout.
The handler itself has no local timeout, yield or abort check in its visible
loop. Possible asynchronous firmware recovery is not modeled. Subtype 0 is
rejected with -3 before any register read; an absent opcode-table entry also
returns -3. The RISC-V engine base is `0x10019180000`; its relationship to host
physical resources is not established and it must not be mapped as host MMIO.

The original initializer installs these nonzero table entries in the inspected
firmware image (other opcode meanings remain open):

| Opcode | Handler code VA |
| --- | --- |
| 3 | 0x102d2a |
| 4 | 0x102c16 |
| 32 | 0x102816 |
| 33 | 0x102a82 |
| 35 | 0x102ada |
| 37 | 0x102b62 |
| 127 | 0x10282e |
| 128 | 0x10285e |
| 221 | 0x102dfe |
| 222 | 0x10288c |
| 223 | 0x102a00 |
| 224 | 0x102978 |
| 225 | 0x102904 |

This proves the diagnostic INIT_IL's polling behavior under supplied RAM status,
not actual NPU readiness, firmware boot, instruction-fetch DMA, task completion,
or inference. Future host recovery must not free task DMA merely because its
own wait timed out while firmware may still be running a poll.

## Diagnostic loadable

Runtime RVA `0xa6610`, raw offset `0xa5810`, size `0x5a9fc8` (5,939,144 bytes).
The append-diagnostic path at RVA `0x15f38..0x15f5c` passes that pointer and exact
size into the append-loadable virtual method at slot 0x218. It begins with root
offset 36 and identifier `NVDA`; its inline version bytes are 0,8,0.

A bounded projection using the open NVDLA-v1 schema yields:

- One DLA task, instance -1, using nine address IDs.
- Nine memory entries and nine address entries, no pre/post events.
- Seven blobs, including `task0-addr0` (128 bytes), `task0-db` (576),
  `task0-ib` (124,928), positive/negative/statistics buffers (27,264 each), and
  `working_buffer` (5,729,504).
- Six tensor descriptors and one submit group containing task 0.

The old open schema's declared version is 0.7.0 and it has no file_identifier
statement. Therefore the modern 0.8.0 format is only provisionally projected;
matching fields and valid bounds do not prove the whole schema is unchanged.
The builtin image is an analysis/test candidate, not a model validated on Linux.
Runtime exports/list operations support host-side parsing of memory, task and
address lists; exact transformation into kernel/firmware descriptors remains open.

## Hardware access boundary

The historical DSDT's DLA0 block contains HID/CID/revision/CCA/resources and a
_STA returning 0 when DLAE is set, otherwise 0xf. It declares no local _PS0,
_PS3, _PR0 or _PR3. A global device-property package references DLA0 as “dla”.
An archived/candidate audit now covers all five SSDTs and parent scopes; no DLA
power methods or references were found there. Live identity still needs checking.
See [the detailed audit](acpi-audit.md).
The read-only live-table copy currently awaits system authentication. No module
has been bound, no NPU MMIO read/write issued, and no firmware boot attempted.

## HAL-1 interrupt-mask policy after method-2 idle

`emulate_irq_mask.py` executes the complete original KMD callback at RVA
`0xd49800`, its three original subobject forwarding thunks, and HAL-1 register
accessors. Only logging, the indirect-call guard and generic register I/O are stubbed.
The original stack-cookie helpers now execute with a synthetic cookie. The test pins the KMD hash and checks 144 combinations of
enable argument, saved mask, current mask, ownership mask and accessor errors.
All accesses occur in emulator RAM; no device registers are read or written.

The independent constructor/thunk/accessor join in
`decode_transfer_registers.py` now includes:

| Subobject slot | HAL slot | Access | Resource-relative offset | Role inferred from operations |
| --- | --- | --- | --- | --- |
| 0xb8 | 0x298 | write | 0xd20 | interrupt mask set |
| 0xc0 | 0x2a0 | read | 0xd28 | current interrupt mask |
| 0xc8 | 0x2b0 | write | 0xd24 | interrupt mask clear |

The enable argument is truncated to eight bits. If nonzero, the callback
ORs `0xc0` into HAL+0x49c and writes the resulting saved mask to 0xd20. If
zero, it reads 0xd28, intersects with subobject+0x54, replaces the saved mask
only if that intersection is nonzero, then writes the intersection to 0xd24
even when it is zero. A read error skips that write. The callback always
returns zero, suppressing both read and write errors. Any future Linux
implementation must propagate callback failures rather than copying this
Windows error policy.

The method-2 start orchestrator supplies subobject+0x50 as this argument
after the successful upload/reinit request and idle wait. Those flags, the
initial saved/ownership masks, IRQ routing and the eventual unmask/rearm
lifecycle are not yet established on hardware. This does not justify enabling
all interrupts, treating `0xc0` alone as a complete setup, or adding a
CPU-control start write to the SROOT boot path. The response ACK helper clears
SWGEN1 in the same 0xd24 mask register. Firmware-side rearming for basic
HAL-1 mailbox replies is now established below; physical routing and the
complete host IRQ lifecycle remain unverified. No physical backend is enabled.

## HAL-1 firmware rearms each mailbox response

The earlier inference that masking SWGEN1 on ACK necessarily requires an extra
host unmask operation was incomplete. Original RV32 mailbox routine `0x1021b2`
saves mailbox0/1, writes the reply, then executes:

- `0x1021d6`: write `0x80` to internal `0x01400d20` (mask set);
- `0x1021da`: write `0x80` to internal `0x01400000` (IRQ set);
- wait for mailbox0 ACK `0xd1a0cafe`, then restore both mailboxes.

These are internal firmware addresses; they do not establish host physical
resource addresses. The instruction sequence and exact operand values are
observed from the matching HAL-1 raw image, not inferred from Jetson alone.

`emulate_profile1_commands.py` now models mask-set/mask-clear and IRQ-set/clear
state in RAM, delivering a synthetic SWGEN1 only when pending AND enabled.
`test_response.py` runs four commands through the Linux C method sender, original
RV32 command/ping/mailbox routines and Linux C response helper **without resetting
firmware RAM or interrupt masks between commands**. Challenges 1, 0x11111111,
0x40000000 and 3 yield 4, 0x44444444, overflow-error response and 12. Each command
writes method data before method ID, and each reply executes mask-set before
IRQ-set. After every C ACK, the modeled SWGEN1 mask and pending bit are zero;
the following original firmware invocation sets the mask again. The four
responses and original mailbox restoration are checked.

A counterfactual model that ignores mask-set side effects delivers no interrupt:
the original firmware remains in its ACK wait within the instruction budget.
This negative control verifies that the test no longer calls the host handler
unconditionally on IRQ-set. It is not a physical device test. DMA, interrupt
routing, scheduler entry, method-register hardware delivery and platform
coherency remain synthetic; firmware bookkeeping/logging/hang queries remain
stubbed. The mailbox protocol now supports repeated offline commands without
an invented host rearm write. This does not establish complete Linux IRQ setup,
task execution, inference or firmware boot.

## HAL-1 firmware IRQ initialization and host route-mask capture

`emulate_profile1_irq_init.py` executes the complete matching RV32 routine at
`0x100370`, including its original table-programming callee `0x1009b6` and tail
call `0x1021a6`. **No function is stubbed in this experiment.** Code/data are
loaded from the hash-pinned HAL-1 image, while MMIO, stack and initial CPU CSR
state are synthetic. This is one initialization routine, not the reset entry
or a complete firmware boot.

The routine makes 41 four-byte MMIO writes. Its first 22 writes select ten
entries through internal offsets 0xf24/0xf28, bounded by writes 0xf20=4 and
0xf24=0x80000000. The test pins all ten values and the complete following
19-write sequence. Among those observations:

| Internal register offset | Written value |
| --- | --- |
| 0xd2c | 0x1d0 |
| 0xd30 | 0x100 |
| 0x0c | 0x1f2c |
| 0xd20 | 0x1f85 |
| 0xd60 | 0x3f |
| 0xd64 | 0xffffffff |
| mailbox0 / mailbox1 | 0 / 0 |

Four initial values at offset 0xf08 verify the original read/modify/write
`(initial & 0xff3fffff) | 0x800000`. With otherwise zero synthetic state, the
resulting CPU CSRs are mstatus=0x2008, mie=0x888 and mtvec=0x1002dc. These are
firmware observations, **not a host register initialization recipe**. Do not
duplicate these writes from Linux merely because a successful emulator trace
contains them. Hardware effects and table-entry meanings remain unverified.

The original Windows common-loader slice `0xd43c48..0xd43c98` reads offset
0xd2c through subobject slot 0xa0 into subobject+0x54, then calls the already
recovered HAL mask callback with enable=0. The extended `emulate_irq_mask.py`
executes this slice, the original read thunk/accessor and original callback.
Its main case feeds the **actual output** of the RV32 initializer above into
the synthetic Windows register backend: route/ownership value 0x1d0 and mask
0x1f85 yield mask-clear 0x180 and saved-mask 0x180. Additional zero and alternative
route-mask cases verify the intersection rather than assuming a constant.

Thus subobject+0x54 is populated from a device register in this common-loader
branch; it is not simply a fixed 0xc0 ownership constant. The separate method-2
resume/reinit branch can bypass this slice, so this does not prove every
lifecycle path refreshes that field. Constructor/configuration of the enable
flag at subobject+0x50, the exact host/target routing semantics of 0xd2c/0xd30,
and assignment of ACPI GSIs 463/800 remain open. The ACPI GSIs must not be
chosen by analogy to a Jetson interrupt number. No Linux device binding,
physical IRQ registration, MMIO access or firmware boot was performed.

## Default opaque upload selection and corrected ARM64 stack modeling

`emulate_boot_variants.py` now executes the original HAL-1 initializer, complete
upload-selector function `0xd45cb0`, variant getter `0xd46780` and table provider
`0xd46760`. It feeds the constructor-derived default remapper 1 from
`emulate_boot_policy.py`. The chip-ID getter is stubbed to the independently
classified P16 value 0x254; the auxiliary-request callback is the test boundary.

For default mode 1, group 0 variant 0 is requested first: RVA 0x1405800,
67,508 bytes, SHA-256
`0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4`.
This is an **opaque SROOT input**, not the 66,816-byte raw RV32 research image.
Authentication/encryption/signature structure and physical acceptance are still
unknown. Modes 0/2 select the other group with 77,492-byte inputs.

Twenty-two cases verify first success, fallback to variant 1, exhaustion through
null variant 2, a remembered successful variant, initial indices 1/2/3 and an
invalid remapper. On a transport failure after a prior successful upload, Windows
does **not** continue to another variant. On two transport failures with no prior
success, null variant 2 changes the returned status to 0x1f, rather than preserving
the last transport error. This behavior is recorded, not adopted as a Linux
retry or firmware-security policy.

The existing extractor now supports `--sroot-variant 0/1` with remapper 1 as its
default (explicit `--remapper 0/1/2` is available). It verifies the exact KMD hash,
0x254 record, pointer/length, full blob hash and leading 0xc0302 tag. Six extraction
cases are compared to the original selector outputs, and existing outputs are
never overwritten. No vendor binary is committed.

This analysis exposed an error in the older ARM64 emulator setup: helpers
`0xd57b60` and `0xd57b80` were replaced with RET, but they allocate/release a
16-byte stack-cookie frame. A nested getter's output could consequently overwrite
saved registers, corrupting later upload-error paths. The shared
`arm64_stack_cookie.py` installs the **original** helper instructions and a
synthetic process-global cookie. All nine affected existing emulators now use
it, and their complete vendor-backed regression suite passes again. The new
upload-selector test also uses the original helpers. Previous traces were
isolated observations under stubbed helpers; the rerun with correct stack
semantics is the current evidence. No OS security mechanism or device is invoked.

## HAL-1 reset-to-scheduler execution with original context

`emulate_profile1_boot.py` now enters the raw HAL-1 image at **0x100000** rather
than calling an isolated function with an injected context. No function is
stubbed. The original reset code clears CPU registers, installs its temporary
trap vector, loads stack top **0x187300** from the inline word at 0x1000b0 and
calls initializer 0x10032a. Code/data come from the pinned raw image. The global
context pointer at 0x1862bc already contains **0x180100** in that image and is
not replaced by the test.

With zero-initialized synthetic MMIO, execution reaches original 0x100e1e,
writes bit 0 to internal offset 0x7c and waits at 0x100e4a..0x100e4e until that
bit clears. The explicit register model can clear this bit immediately or after
3/17 busy reads. Under those supplied conditions, all 13 recorded initializer
and scheduler entry points execute in order, including the original IRQ setup,
and execution reaches WFI at **0x102194**, with SP=0x1872b0, mstatus=0x2008,
mie=0x888 and mtvec=0x1002dc. The test stops **before** executing WFI because
asynchronous interrupt delivery is not modeled. There are 95 original MMIO
writes before that point. Mask set/clear effects are modeled in the current
mask at 0xd28.

Without the model's bit-clear event, the same original code remains at the
0x7c poll through the 200,000-instruction observation budget. This establishes
a software handshake dependency, not its physical completion time, register
name, reason or recovery policy. It is not evidence that a Linux host should
clear that bit itself. All MMIO is emulator RAM; no NPU device is mapped.

`test_response.py` additionally runs four consecutive Linux C method-send / C
ACK round trips **after this reset initialization**, preserving the original
context pointer and initialized data. Logging, command bookkeeping and hang
checking now execute their original RV32 code. Only DMA functions 0x104ed8 and
0x105004 remain stubbed to bounded RAM copies. The sequence covers ping inputs
1/0x11111111, overflow input 0x40000000 and a following successful ping 3.

The initialized mask is 0x1f85 before each reply; the C ACK clears only SWGEN1,
leaving 0x1f05, which is preserved across subsequent commands. This supplements
the earlier zero-initial-mask model, whose final mask was zero. The response
words, input/output values, mailbox restoration and remaining mask/pending bits
are all checked. The initial version of this test entered the dispatcher explicitly with method
register values. The extension below now exercises the original trap handler
under synthetic interrupt/FIFO injection; physical delivery remains unverified. No opaque SROOT image acceptance,
physical boot, DMA coherency, kernel IRQ delivery, task execution or inference
is claimed.

## HAL-1 external trap, method receive registers and DMA-channel restore

The reset-initialized command round trip now enters **the original mtvec at
0x1002dc**, not the dispatcher. The synthetic external-machine-interrupt model
supplies mcause=0x8000000b, pending IRQSTAT bit 2, and machine trap-entry CSR
state. MEPC is a synthetic stop address; this tests ISR return without claiming
to resume the real scheduler. No physical interrupt is delivered.

Original handler 0x1005f2 reads the masked pending sources and calls 0x102508
for bit 2. That function executes the following receive sequence:

1. Read method ID from internal offset **0x68**.
2. Read received method data from internal offset **0x64**.
3. Write **1 to 0x74** to acknowledge the method entry.
4. Tail-call original dispatcher 0x102232 with those method/data arguments.

The Linux C sender's host write-data register is **0x6c**, whereas the firmware
receive-data register is **0x64**. The test therefore supplies an explicit
one-entry FIFO model between those sides. This is not proof of physical FIFO
capacity, fullness transitions, register aliasing, device-side delivery timing
or safe concurrent submissions. The C sender's original data-before-ID ordering
is checked separately in the same trace.

After processing the reply, the ISR calls original channel-state restore
0x104dfe -> 0x104dae. That path calls 0x100262, which polls **offset 0x118 bit 1**
before completing channel restoration. The synthetic model supplies value 2
there, explicitly representing the required ready condition. The actual
firmware then executes its original interrupt epilogue and **mret at 0x100324**.

`test_response.py` verifies four consecutive C send / original trap / original
command / C response-ACK / original mret rounds after reset initialization.
Every round checks method reads, entry acknowledgement, response, one trap
entry/return, and restoration of **all x1..x31 integer registers**, including
SP. Only the two DMA transfer functions remain stubbed to RAM copies; trap,
bookkeeping, logging, channel restoration and mret execute original code.

A negative control with offset 0x118 held at zero still processes the first
ping and obtains its host ACK, but remains in the DMA-ready poll before mret.
No further command is submitted and no ISR return occurs within the instruction
budget. Thus a mailbox reply does not by itself establish that all firmware
interrupt cleanup has finished. This is another concrete reason to preserve
DMA lifetime/quiescence handling in a future Linux backend. The observation
budget is not a firmware timeout and the model does not establish recovery.

The Linux host's ACPI GSI routing, firmware hardware boot, actual DMA status
behavior, method FIFO and coherent mapping remain unverified. These tests
establish the firmware's software receive path under explicit model conditions.

## HAL-1 ping round trips with original DMA functions

The strongest command test now leaves **all firmware instructions unchanged**:
reset initialization, trap entry/return, logging, command handlers, DMA setup,
DMA issue and DMA completion polling execute their original RV32 code. The
`dma_register_model` option in `emulate_profile1_commands.py` removes the final
0x104ed8/0x105004 function stubs. Existing isolated-function tests remain as
separate references.

The bounded synthetic model watches writes to internal DMA command offset 0x118
and uses the programmed registers rather than transfer-function arguments:

- External base: `(reg[0x128] << 40) | (reg[0x110] << 8)`.
- External address: that base plus reg[0x11c].
- DMEM address: 0x180000 plus reg[0x114].
- Transfer size: `4 << ((command >> 8) & 7)`.
- Observed read command: 0x2000; observed write command: 0x2020.

The decoder accepts only the observed DMEM/context-2 command family and bounds
transfers to the explicit synthetic host payload and mapped firmware RAM
(four bytes for ping; the task test below supplies a larger backing buffer).
It does not implement a general bus, IOMMU, other DMA contexts or instruction
memory DMA. Unsupported commands/ranges fail the test rather than silently
performing an arbitrary copy. Command size/direction interpretation agrees with
the original functions' inputs and the resulting challenge/output values.

A command-register write performs a synchronous RAM copy in the model. A later
read of 0x118 supplies status 2 (idle, not busy). This fake completion timing is
explicit; it does not establish a physical device's status transitions, cache
coherency, error bits or ordering requirements. The original code independently
performs its busy/idle polls and channel save/restore operations.

`test_response.py` verifies the same four C send / original trap / original DMA /
C ACK / original mret rounds after reset. There are exactly **seven** original
DMA commands: four reads to scratch DMEM 0x1833ac and three writes back to the
host. Overflow input 0x40000000 is read but not written back; the following ping
still succeeds. Full command records, values, responses, all interrupt returns,
mailbox restoration and remaining mask/pending bits are checked. No firmware
function is stubbed in this added test path.

A negative control returns zero instead of idle status: the original firmware
waits before its first DMA transfer, issues no modeled copy, sends no reply and
leaves the host input unchanged through the observation budget. This verifies
that readiness is consumed by original code rather than bypassed by a stub.

The environment still supplies the reset handshake, method FIFO, machine trap
entry, DMA copies/status and host interrupt dispatch. Thus this is stronger
software integration evidence, **not physical DMA, NPU bring-up or inference**.
ACPI/IORT identity, DMA requester/coherency, SROOT boot acceptance and actual
interrupt delivery remain prerequisites for the Linux hardware backend.

## Linux task submission through reset, trap and original DMA

`test_response.py` now also compiles the existing Linux C task serializer into
its integration library. It builds a queue-0 v2 descriptor with next=0, one
address entry, timeout=1000000 and address-list pointer HOST+256, within a
zero-initialized 256-byte descriptor backing. That address-list target is a
synthetic future-task reference; it is **not** fetched or executed by this test.

The Linux C sender submits method **0x307** and descriptor address HOST>>8.
After original reset initialization, the original machine trap reads the method,
acknowledges its FIFO entry and executes the task-submit code. Original DMA
functions program four host-to-DMEM transfers of **32, 16, 8 and 4 bytes**,
copying the exact 60-byte C header from HOST into first task slot **0x180200**.
All addresses, directions, lengths and cumulative offsets are checked.

The task slot's header matches the Linux bytes. Its original metadata records
state **2 at slot+124** and the host descriptor address at **slot+128**. The
original enqueue helper at 0x103e6e stores the task pointer into queue0 and
increments its count. In this reset-initialized model, queue0 at **0x180100**
has capacity **2 at +16**, count **1 at +20** and slot pointer 0x180200 at +8.
This is an observed initial firmware configuration; later host setup or other
firmware paths could change queue configuration. It is not a universal Linux
queue-depth declaration.

The Linux C response helper receives **0x702**, acknowledges it, and the
original ISR returns through mret with its integer registers restored. The
whole firmware code region is compared to the original bytes and remains
unchanged. No firmware function is stubbed; hardware register/DMA effects are
still the explicit synthetic models described above.

A negative case changes only descriptor version byte +8 to 255. Original code
returns response **0x90701**, leaves queue count zero and the first slot's state
word zero. It still fetches the header before rejecting it. This extends the
earlier isolated common-header checks to the initialized queue and complete
command/response path, rather than assuming a positive parser result means
successful enqueue.

Neither case resumes the scheduler after interrupt return. **Task acceptance
is not task completion or inference**: address-list/model loading, instruction
execution, postactions, error reporting during execution and hardware compute
are not exercised here. Buffer lifetime must extend through actual completion
or quiescence; the immediate 0x702 mailbox ACK is insufficient for reclamation.

## Scheduler execution error after task acceptance

The command emulator can now resume the **original scheduler** after command
interrupt return. It first verifies the trap restored every integer register,
then continues at the original WFI helper's return instruction 0x102198, with
the original scheduler stack/caller state. It does not call a task handler
directly. Machine interrupt delivery and this wakeup remain synthetic. The
legacy `returned` result describes command/trap return before this optional
scheduler phase; `scheduler_wfi` separately reports the later scheduler wait.

The integration test builds a fenced descriptor with the Linux C serializer,
a one-entry address list from the Linux C serializer, a deliberately invalid **zero-filled
256-byte network**, and a sentinel fence word. The host payload is 1024 bytes:
descriptor backing at +0, address list at +256, network at +512 and fence at
+768. No useful model or numerical computation is claimed for this input.

After the C response helper ACKs submission response **0x702**, the scheduler
executes 0x104766 -> original task execution 0x1014f6. The same original
0x7c bit-0 handshake seen during reset occurs again during task execution;
the explicit register model can acknowledge it or hold it pending. With that
synthetic acknowledgement supplied, the observed path continues through
postaction handling 0x103d56 and dequeue 0x103f9c, then returns to scheduler WFI.
No firmware instruction is changed and no firmware function is stubbed.

The C response helper receives and ACKs a **second** response,
**0x8003070a**, carrying the same descriptor address HOST>>8. This is the
observed task-execution error for the zero network, distinct from the earlier
submission ACK. The test does not generalize all message-type/error/flag meanings
from this single frame. All 13 programmed DMA commands are host reads (two
60-byte descriptor fetches, one eight-byte address list, one 256-byte network);
there is no fence write. Queue count and task slot state become zero, while the
fence remains **0xdeadbeef**, not requested value 0x12345678. This independently
checks the earlier finding that failed execution skips terminal postactions.

The negative control withholds the scheduler's 0x7c acknowledgement. It gets
only 0x702, remains in the original poll through the observation budget, retains
queue count **1** and slot state **3** (observed running state), and leaves the
fence unchanged. It supplies no execution-completion response and does not
reach scheduler WFI. This is not a firmware timeout or recovery mechanism.

These results demonstrate why a Linux backend must separate submission ACK,
execution status and fence observation: neither the acceptance response nor an
unchanged fence can establish completed/quiescent work. Successful task execution,
real firmware boot, hardware status transitions, DMA/IOMMU/coherency and kernel
interrupt handling remain unverified. No NPU device was accessed.

## Diagnostic INIT-IL through the initialized scheduler and register DMA

The integration test now extracts the **original network and instruction blobs**
from the hash-pinned builtin diagnostic NVDA loadable (SHA-256 51d26d...c28543,
listed above). It uses the same bounded provisional field projection as the
existing diagnostic inspection. The 128-byte task0-addr0 network is unchanged,
padded to the already observed 256-byte firmware fetch extent; the complete
124,928-byte task0-ib instruction blob is unchanged.

The Linux C task and address-list serializers build a nine-entry descriptor
layout in synthetic host RAM. Address 0 points at the network and address 1 at
the instruction blob. The remaining seven entries are explicitly **zero and
not provided**: this test stops before their use and does not represent the
full diagnostic allocation layout. Host backing is 125,952 bytes, page-mapped
without overlapping emulator stop memory. The DMA model still bounds every
transfer to the explicit payload, not merely the rounded mapping size.

The full original path executes: reset, C method send, machine trap, descriptor
DMA, queue insertion, C ACK, mret, scheduler task pickup, descriptor reload,
address-list fetch, network fetch, opcode table setup and diagnostic INIT-IL.
No firmware function is stubbed and the code region remains byte-for-byte
identical to the raw image. There are **15** programmed DMA commands: eight
for two 60-byte descriptor fetches, two for the 72-byte address list, four for
the 256-byte network and one 256-byte instruction-cache read at IB+122880.

The test supplies value **1** at internal engine offsets **0x7008, 0x5000 and
0x6000** immediately before scheduler resumption. Original INIT opcode-3
handlers read those three values once each. Execution reaches the checkpoint
at **0x101052**, immediately after the INIT slice returns, with **status 0**.
The final cache fetch programs command 0x2600, source HOST+1024+122880,
destination DMEM 0x184400 and length 256. All transfers are reads; the original
host payload, including the untouched fence, is checked byte-for-byte.

Only the submission response 0x702 has arrived at this checkpoint. Queue count
is still one, task state still three, and no execution completion or fence is
claimed. The checkpoint is an explicit observer stop in original code, not a
firmware return or completion event.

With the supplied engine-ready values omitted, original code remains polling
0x7008 through the observation budget, never reaches 0x5000 or the checkpoint,
and retains the queued/running task. This revalidates readiness dependence in
the complete scheduler context rather than only the earlier isolated handler.
It is still a synthetic hardware input, not measured readiness or a timeout.

Further network processing, the missing diagnostic allocations, accelerator
programming, numerical output and complete task success remain unverified.
This is a complete software path **to INIT-IL**, not a completed diagnostic or
physical inference. ACPI/IORT, real DMA and IRQ routing, and opaque SROOT boot
acceptance remain separate hardware-backend prerequisites.

## All projected diagnostic allocations and first engine-wait point

`inspect_loadable.py` now reports the memory-entry content names and content
byte offsets in addition to sizes/alignment. These are read through the same
bounded **provisional v1 field projection**, not a newly established full modern
schema. `diagnostic_fixture.py` builds a hash-pinned RAM fixture containing all
nine projected allocations, copies every original content blob at its declared
offset, preserves their logical sizes, and returns the nine address entries.
Scratch/output allocations and cache-read padding are explicitly zero-initialized.
This is a test fixture, not a physical firmware/model installer.

The resulting payload is **8,428,800 bytes**. It includes the 5,729,504-byte
working buffer, 2,471,680-byte scratch allocation, three 27,264-byte buffers,
network/instruction/DB blobs and the final 32-byte allocation. Each allocation
honors its declared 256/1024/16384-byte address alignment. Explicit backing is
rounded to 256 bytes, with at least 256 for the observed network cache read;
this padding policy does not change the loadable's logical sizes.

Reset-initialized emulation now places its synthetic stop address at 0x40000000,
separate from the host payload range. The prior stop address would have fallen
inside this larger contiguous host fixture. This is an emulator address-space
change, not a hardware address or Linux DMA allocation policy. Original firmware
code and data placement remain unchanged.

The Linux C task/fence and address-list serializers populate the fixture. Original
reset, trap, scheduler, DMA and diagnostic code then execute with the same three
explicit ready inputs as the INIT test. No instruction or function is replaced.
The test observes **19 original DMA commands**, including a 64-byte read from
address entry 2 (task0-db) to DMEM 0x184300. It records all scheduler MMIO writes:
the original firmware writes eight low/high address pairs to internal offset
**0x17048**, matching the first eight fixture addresses, and writes **1 to
0x18074**, alongside further engine configuration. These are observed writes;
the complete register meanings and hardware effects are not established.

Execution next reaches WFI with return address **0x101424**, inside the model's
wait loop at 0x101420. The fixture's host bytes/fence remain unchanged; queue
count is **1**, task state **3**, and the only mailbox reply is submission ACK
0x702. No synthetic accelerator-complete interrupt or numerical output is
injected. Thus the task has reached a device-wait point and remains running.

The result key `scheduler_wfi` reports a WFI observation, **not idle or task
completion**. Its `wfi_return_address`, queue and task state distinguish this
in-flight engine wait from the earlier empty-queue scheduler wait. A Linux
backend must not reclaim work just because firmware executes WFI.

The complete projected memory layout now replaces the earlier zero placeholders
in this additional test; the smaller INIT-only tests remain separate negative
and prefix checks. Equivalence to Windows runtime allocation/binding semantics,
actual accelerator completion and correctness, DMA coherency, physical boot and
IRQ routing remain unverified. No hardware device was accessed.

## Native Linux response-field decoder versus the Windows parser

The Linux C response component now exposes `n1x_dla_decode_response()` alongside
capture/clear/ACK. It extracts message, command and status from bits 7:0, 15:8
and 23:16 of the captured word, and preserves the upper byte and mailbox1 data
verbatim. Decoding performs no I/O and does not make a completion/reclamation
decision. Return zero means the bytes were decoded, not that firmware work
succeeded. Unknown field values are retained for the caller to interpret.

`test_response_decode.py` executes the complete original Windows HAL-1 parser
at RVA **0xd48910**, its original HAL initializer, mailbox0 forwarding thunk,
mailbox0/1 accessors and stack-cookie helpers. The generic register-read boundary
and indirect-call guard are synthetic; logging is disabled or stubbed. No device
or Windows OS code is invoked.

**1,028 distinct response words** cover all 256 byte values independently in
each position plus actual observed frames. The Windows parser's three output
fields agree with the Linux C decoder for every case. It ignores the upper byte
for these outputs; preserving that byte in Linux does not establish its meaning.
Two original-reader error cases prove the Windows parser leaves output fields
unchanged when either mailbox read fails. Linux C argument-error checks preserve
its output on invalid input; capture-I/O failures remain covered separately.

The scheduler-failure integration test also runs its actual captured firmware
frames through the new native decoder:

| Observed frame | Message | Command | Status | Upper byte |
| --- | --- | --- | --- | --- |
| Submission 0x702 | 2 | 7 | 0 | 0 |
| Execution error 0x8003070a | 10 | 7 | 3 | 128 |

Both retain the descriptor-address data word. These observations let a future
Linux backend distinguish the two events without losing the original bits.
They do not define every message type, task-success signal, flag or error code.
Outstanding-task association, deadlines, recovery and DMA quiescence still need
implementation and hardware validation; neither frame parsing nor a zero status
alone authorizes freeing an active task's buffers.

## Original Windows IRQ error policy and SWGEN0 counter handling

`test_response_decode.py` now additionally executes the complete original
HAL-1 IRQ callback at **0xd48ce0**, using its actual initializer, mailbox parser,
accessors, message predicates and debug-register helper. The IRQ mask argument,
lock success, context and registers are synthetic. OS/logging, dump collection,
report emission and memory-clear boundaries are stubbed. In particular this
does not execute a real recovery, reset, Windows event or device operation.

Eighteen response cases cover the observed submission/descriptor-error/task-error
frames plus message bytes 2/8/9/10 combined with status bytes 0/3/15/255. Under
the tested zero feature flags:

- Submission 0x702 reads both mailboxes and writes ACK 0xd1a0cafe without a dump.
- Frames 0x90701 and 0x8003070a both set context byte **+0x5ac** to 1, invoke
  dump collection with code **0x8a**, write the ACK, then emit the report.
- Message 9 uses report code **0x98**; otherwise message 8 **or** status 15
  selects **0x99**. Other tested nonzero/non-255 statuses select **0x8a**.
- With tested message 2/10 and status 0/255, there is no dump/report in this path.

The ordered callback trace pins **dump -> ACK -> report** for errors. The
original callback returns **zero even on these error paths**. Context +0x5ac
is an observed state byte also used elsewhere; this experiment does not assign
it a universal recovery-ready/error-bit meaning. A zero callback return and
an ACK are not evidence of successful work or completed recovery. Report-code
names and complete error taxonomy remain unestablished.

The SWGEN0-only path (IRQ bit 6) reads neither mailbox and performs no ACK write.
It decrements context counter **+0x5b4 only when positive**. Tests with initial
0/1/2 yield 0/0/1. Combined bits 6+7 decrement the counter and process a normal
mailbox reply in the same invocation. The counter's full lifecycle and relation
to Linux task ownership are not inferred from these injected initial values.

The callback-level ACK trace intentionally differs from Linux's complete
capture/clear/ACK helper: Windows enters this callback after earlier IRQ layers.
It does not imply that mask/status clearing may be omitted by a Linux backend.
Remaining queue association, hardware quiescence, physical IRQ setup and recovery
must be implemented and verified before reclaiming DMA-backed work.

## Complete original initial/later loader ordering

`emulate_common_loader.py` executes the entire original function at RVA
**0xd439c0**, the selector-1 HAL initializer, contiguous constructor defaults,
original method-2 start orchestrator and idle-wait helper. HAL transport,
route, mask, ping and optional callbacks remain recording stubs. Seventeen
cases verify the following paths and fault behavior. They do not establish a
live Windows cold/resume invocation or physical firmware boot.

The constructor sets byte +0x47c=0, +0x474=1, +0x47d=1, +0x47e=1,
+0x486=0 and u32 +0x480=1. Method 2 is the independently verified default.
With +0x47c=0 the common loader always invokes upload slot +0x408, including
when +0x486=0. The successful sequence is:

1. Upload -> original IDLESTATE wait.
2. Capture route via subobject slot +0xa0 into +0x54; disable masks via HAL +0xd8.
3. Ping via HAL +0x138 (`0xd48110`).
4. With constructor defaults: region 5 via +0x160 (`0xd49c20`), then
   +0x78 (`0xd46300`), +0x90 (`0xd47830`), +0x88 (`0xd49570`),
   and +0x438 (`0xd496e0`). The latter functions' complete behavior is not
   established by recording their invocation.
5. Apply IRQ enable via +0xd8, set byte +0x47c=1, clear counter +0x5b4 and
   loader/error byte +0x5ac.

With +0x47c=1 and +0x474=1, the common loader instead invokes +0x420
(`0xd47430`). Its +0x486 flag chooses reinit (zero) or upload (nonzero), then
idle, region 5, +0x88, +0x438 and IRQ configuration. This path bypasses the
initial loader's route capture and ping. Thus reinit is not the first-load
recipe, and an unconditional upload-then-reinit sequence is not supported.

Upload/idle/ping errors on the initial path prevent subsequent initialization,
leave +0x47c=0 and +0x5ac=1, and invoke mask-disable cleanup. A route-read error
is ignored and leaves ownership at the initial 0xffffffff. Mask and tested
optional-callback return errors are also ignored. These are Windows behaviors,
not sound success/quiescence rules to copy into Linux. The Linux backend must
propagate transport/configuration errors and must not free live DMA storage
merely because this orchestration returned an error.

## HAL-1 region-5 serialization and local ring setup

The original HAL-1 dispatcher at 0x102338 recognizes region IDs
1/2/3/5/6/7/8. Region 5 calls **0x106a4a**, which reads a packed 16-byte record
into **0x1859d0**: ID at 0, u64 address at 4, size at 12. With nonzero address
and size it reads eight bytes of header into **0x1860f8**. If both u32 header
words are zero, it initializes its local pair to **8/8**. Setup itself does not
write that normalization back to host memory. No semantic name is assigned to
the ring solely from this layout.

`test_profile1_regions.py` compares five actual C records with the original
Windows builder, then passes them through the unmodified HAL-1 reset, trap,
command, DMA-function and ACK paths under the explicit register/DMA model.
Regions 1/3 succeed; three region-5 cases cover zero/zero, 8/8 and 64/128 headers
at different addresses/sizes. Each returns **0xb02**, leaves the host payload
unchanged and retains byte-for-byte original firmware code with no function
stubs. Region 5 uses DMA extents **4, 16, 8** bytes; the original local descriptor
and header match the supplied values/normalization.

The C serializer now accepts region 5 and rejects sizes below eight bytes
before changing its output. It still rejects region 7 because that record
format/lifetime has not been revalidated. A preliminary region-7 probe exceeded
the deliberately bounded synthetic host mapping; it is not evidence of an
unsupported firmware region. Existing HAL-2 region-5 rejection remains valid
for that different image.

This proves registration/local ring setup in the **raw HAL-1** image, not ring
production, consumption or hardware execution. The code identity of the opaque
SROOT upload variant has not been established by these tests. Physical DMA,
firmware boot and numerical inference remain unverified.

## Post-upload configuration method boundaries

`emulate_postinit_commands.py` executes original Windows callbacks at
0xd46300 (HAL +0x78), 0xd49570 (+0x88), and 0xd496e0 (+0x438) to recording
method/cache/delay boundaries. Fourteen cases cover valid/missing CPU and
logical addresses, a shifted address of zero and method-send errors.

| HAL slot | Windows RVA | Command | Encoded data / context field |
|---|---|---:|---|
| +0x78 | 0xd46300 | 22 | u64 +0x500 >> 8; CPU buffer at +0x4f8 |
| +0x88 | 0xd49570 | 21 | u64 +0x500 >> 8; same CPU buffer |
| +0x438 | 0xd496e0 | 23 | u64 +0x4f0 >> 8 |

Command 22's wrapper sends first, then on success calls the delay helper with
100 and cache helper with `(CPU,4096,0)`. Command 21 calls cache helper
`(CPU,4096,1)` and delay 100 before send, then delay 100 and `(CPU,4096,0)` on
success. Failed sends skip the trailing delay/cache operations. Cache effects
and delay units are not inferred merely from these captured arguments. Both
wrappers return 0x40 for missing CPU/usable encoded DMA address. Command 23
returns 0x1f for a zero encoded DMA address and otherwise directly forwards the
method return. None of these callbacks changes the synthetic CPU buffer itself.
HAL +0x90 at 0xd47830 edits the shared configuration before command 21; its
complete field layout is still being decoded.

The raw HAL-1 firmware independently confirms command 22 exporting and command
21 importing a **252-byte** block at **0x186154**. `test_profile1_config.py`
executes both with the original reset/trap/DMA/ACK paths and unchanged firmware
code, with no firmware function stubs. Each transfer decomposes into **63
four-byte DMA operations** in this layout. A default export can be imported
unchanged. Import mode-byte values 0/1 enter a separate normalization branch;
the test checks its byte/word stores without assigning speculative field names
or physical operating ranges. The default exported mode byte is 2.

Command 23 imports a separate **40-byte** block at **0x186254**; command 24
exports it. A same-firmware-instance test sets a synthetic 40-byte pattern,
erases host memory, and gets the identical pattern back. It observes ten read
and ten write DMA operations with replies **0x1702/0x1802**. These contents are
test data, not a proposed hardware configuration. Commands 21/22 return
**0x1502/0x1602**. Host memory beyond the respective transfer extent is preserved.

This establishes transfer layouts and basic firmware behavior in RAM, not
physical configuration safety, cache coherency or instruction execution on the
NPU. The opaque SROOT image's code identity remains separate from the raw image.

## Original command-23 payload construction

Original Windows function **0xd45390** builds the previously opaque 40-byte
input submitted by HAL +0x438 / command 23. It clears a 40-byte stack record,
sets byte 0 to 1 and byte 1 to `context[0x47d] != 0`. If context u32 +0x490 is
nonzero, it stores an **unaligned little-endian u32 at offset 2** equal to
`provider[0xf4] * 2000`, modulo 2^32. The provider is obtained through the
original global object at RVA 0x1512148, then its pointer at +0x78. The other
34 bytes remain zero. Field units and the live P16 provider value are unknown;
no physical clock/timer value is assigned by analogy.

The function allocates 4096 bytes, clears the full allocation, copies the
40-byte record, calls the cache boundary with `(CPU,4096,1)`, then stores its
CPU/DMA pair at context +0x4e8/+0x4f0. Allocation failure propagates its status;
a NULL CPU result returns 0x1a. Both leave those context fields unchanged.
These are original-code observations with synthetic allocator/cache/OS calls,
not proof of physical allocation or DMA visibility.

`n1x_dla_build_command23()` now implements that exact wire format in C.
`test_command23_builder.py` compares **45 records** with the complete original
ARM64 construction function, including nonboolean flags, disabled scaling,
zero inputs, a multiplication boundary and u32 wraparound. Stack and DMA
storage are poisoned before original execution, so zero fields must come from
the actual memset calls. The original stack-cookie helper runs unchanged.
Two Windows allocator-failure cases and two C output-preflight cases also pass.

One C record is imported by raw HAL-1 command 23 through the unchanged original
reset/trap/DMA/ACK paths, with no firmware function stubs. The local 40-byte
block matches and response is **0x1702**. This validates encoding/import only;
synthetic provider values are test inputs, not settings to publish on the P16.
The serializer does not allocate or submit DMA and requires the eventual
backend to establish the actual provider input and buffer lifetime.
