# Verified contract and open boundaries

All offsets refer to the exact files below. Preferred image bases are
`0x140000000` for the KMD and `0x180000000` for the DLLs. RVA and raw file offset
are distinct. Analysis date: 2026-10-24.

## Identity

| Artifact | SHA-256 |
|---|---|
| NVIDIA 617.42 ARM64 installer | `425a75f54655ddcc8ee6859cec49bc431a17ef18094daaf59f205b159fb6e2bd` |
| nvlddmkm.sys | `73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987` |
| nvdla_runtime.dll | `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b` |
| extracted firmware | `8f5b76232c982a15d44806e7e90b094abcbe3ae377e6b05bda127b492e0fad75` |
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
original accessor emulation. This establishes 20 transfer/core-control slots.

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
This is not a complete audit: live SSDTs and scope extensions must still be read.
The read-only live-table copy currently awaits system authentication. No module
has been bound, no NPU MMIO read/write issued, and no firmware boot attempted.
