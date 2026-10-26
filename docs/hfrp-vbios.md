# NPU power-policy source: VBIOS BIT `l`

This is static and emulated evidence from the pinned Windows KMD, SHA-256
`73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987`.
No physical VBIOS read, firmware upload, power setting or register write is
performed by the scripts or decoder described here.

## Producer chain

The settings pointer used by HFRP configuration is `global+0x78`. The Windows
initialization path at `0xd38d1c` calls constructor `0x195d6d8`, then stores its
result at that offset. The constructor allocates and clears 0x250 bytes and
sets object bookkeeping; it supplies no nonzero frequency/tuning defaults.

HAL+`0x490` is specifically **`RmSocDlaEnableVbios`**, not a generic DVFS
switch. The original constructor sets it to 1. Replay of registry slice
`0xd44130..0xd44168` confirms that a missing value preserves 1, an explicit
value of 1 enables it, and every other successfully read u32 disables it.
`scripts/emulate_boot_policy.py` covers this alongside boot-method/remapper
defaults. This does not identify overrides on an actual Windows installation.
The global initializer gates settings allocation and VBIOS parsing on this
field. However, the normal global path checks the constructor's value before
the later registry override is read. Setting the registry value to zero does
not bypass that earlier acquisition; see [the connected replay](#registry-override-timing).
Disabling it to avoid acquiring the board policy is not a validated Linux
startup configuration.

The initializer checks for a `vbios` registry value through `0xd3b6b0`. If present,
`0xd3bab0` builds a buffer from that property and any numbered continuations.
Otherwise `0xd3d2c0` follows a mapped-memory fallback. Both call `0xd3d010` to
parse the image and release the temporary mapping/allocation afterward.
The fallback's mappings have not been exercised on Linux; this evidence is
not authorization to read arbitrary physical addresses.

Ghidra and ARM64 instruction inspection establish this parser chain:

| RVA | Role |
| --- | --- |
| `0xd3d010` | Scan for `ff b8 42 49 54 00` (`BIT`), check header checksum, find preceding aligned `55 aa` ROM base |
| `0xd3ce70` | Decode BIT entries, find token `0x6c` (`l`), read its nine u32 directory offsets |
| `0xd3c918` | Follow that directory and expand versioned policy tables into the settings object |
| `0xd3d5e0` | Decode format strings: packed byte/word/dword fields become consecutive u32 output fields |

The BIT token supports six- and eight-byte entries. Its data pointer is
relative to the selected ROM base. The nine directory offsets are also relative
to that ROM base. The scanner is limited to a 0x110000-byte input window, but
the Windows implementation does not adequately bound all nested offsets.
It must not be reused directly as a parser for untrusted files.

`scripts/test_hfrp_vbios.py` executes the complete original scanner, directory
parser, policy parser and unpacker, with only memset/logging stubbed. Synthetic
ROMs exercise both entry widths and ROM bases 0 and 0x1000. They confirm the
directory and all fields needed below. The tests also establish that Windows
returns success while silently skipping tables whose first byte is below
0x10. Thus a zero return alone does not prove that usable policy was recovered.

## Relevant subtables

Directory indices are zero-based. All multi-byte input values are little-endian.
The format letters below mean `b` = byte, `w` = u16 and `d` = u32. Each input
value becomes one u32 in the Windows settings object.

| Directory index | Packed format | Minimum bytes | Destination |
| --- | --- | --- | --- |
| 0 | `3w2d` | 14 | settings+`0x74..0x84`; feature mask at `0x80` |
| 1 | `4w` | 8 | settings+`0x88..0x94`; final word copied to HAL+`0x590` |
| 2 | `2w2b3d` | 18 | settings+`0x98..0xb0` |
| 3 | `2w2d3w2d3w` | 32 | settings+`0xb4..0xe0` |
| 4 | `2w1b1d3b2w` | 16 | settings+`0xe4..0x104` |

The first byte's observed gate is `>= 0x10`. Windows uses these same formats
after that gate without a separate per-version layout switch. Synthetic tests
cover 0x10, 0x1f, 0x20 and 0xff; they do not establish that those versions exist
in production firmware. The first two u16 values of each subtable are retained
as raw headers; their individual bit meanings are not inferred from test data.

Frequency subtable bytes 6, 10 and 14 contain the three u32 values used as
requested, entry and exit frequency in kHz. They become settings+`0xa8`,
`0xac` and `0xb0`. The preceding bytes at 4 and 5 remain raw flags.

The tuning subtable's u32 at byte 4 becomes settings+`0xbc`, the raw request
word for command 407. Its remaining nine values become settings+`0xc0..0xe0`
and the nine u32 words of command 408. Their packed widths are:
`u32, u16, u16, u16, u32, u32, u16, u16, u16`. No watt/milliwatt interpretation
or safe tuning ranges have been established.

The gating subtable yields a raw flag at byte 4, a u32 at byte 5, three bytes
at 9..11, then two u16 values at 12 and 14. In Windows, the middle and final
bytes of that three-byte group become settings+`0xf8` and `0xfc`; those feed
outer+`0x31f` and `0x322` after the previously observed fallback logic. The
remaining values are preserved without inventing names or units.

## Linux implementation and limits

`driver/n1x_dla_vbios.c` implements `n1x_dla_decode_vbios_policy()` and its public
struct is in `include/n1x_dla_vbios.h`. It accepts three **already located**
subtable buffers, validates their lengths and the observed version gate, and
decodes fields without alignment-dependent loads. It preserves output on error
and performs no allocation or hardware access.

That three-buffer decoder remains deliberately partial: `boot_tables_valid`
is zero, and it does not supply general/allocation fields. It must not be used
as a complete startup-policy source.

`n1x_dla_parse_vbios_policy()` now locates those tables in an immutable ROM
window of at most 0x110000 bytes. It follows the observed preceding aligned
`55 aa` ROM-base rule, checks BIT header checksum and entry-array bounds, finds
token `l`, validates its declared length and all five nested table ranges,
then decodes indices 0..4. In addition to the three-buffer decoder's fields,
it returns raw general headers/words, the feature mask and four allocation
words, setting `boot_tables_valid=1`. Null/overflowing pointers, missing ROM signatures,
truncated buffers, unsupported table versions and multiple matching policies
are rejected. At most 32 checksummed BIT headers are examined. Both policy
and source-location outputs remain unchanged on failure.

These functions neither acquire ROM data nor prove that a policy is valid for
this board. The caller still must establish matching ROM provenance, apply
only proven override rules and validate the complete startup sequence. No live
module links or calls either function yet.

The differential test generates 32 reproducible mixed-width input sets and
compares **every decoded C field** with the original Windows parser's output,
both through the subtable decoder and through the complete table locator.
Its 76 negative cases cover every truncated table length, unsupported version
gates and null pointers, with unchanged output on rejection. Normal and UBSan
runs pass; the component also builds against the running kernel headers.
Another 35 negative cases check ROM-level bounds, absent tokens, checksums,
zero/overflowing pointers, ambiguity, header-flood limits and null arguments.
Both BIT entry widths and a nonzero ROM base are tested through the locator.

The general feature mask is required for startup: `0xd440d0` passes it to
`0xd435f8`, before applying any `RmSocDlaFeature` override through the same
function. This is an ordered pair of updates, not an OR of the two masks.
Its bit 0 also gates rejection in the global initializer, so merely decoding
the table is insufficient to establish an acceptable policy. The raw general
word at settings+`0x84` and allocation words have no inferred physical units.

```sh
venv/bin/python scripts/test_hfrp_vbios.py
CC='cc -fsanitize=undefined -fno-sanitize-recover=undefined' \
  venv/bin/python scripts/test_hfrp_vbios.py
make -C "/usr/lib/modules/$(uname -r)/build" M="$PWD/driver" n1x_dla_vbios.o
```

## Windows physical fallback, emulated only

The original `0xd3d2c0` function is now replayed through the full original
BIT parser. Global-owner lookup and map/unmap are explicit stubs; the original
32-bit mapped read at `0xd3b228` executes against synthetic RAM. Five locator
words establish the exact address arithmetic, including its 32-bit wrap:

1. Map four bytes at physical `0x1182bc80`.
2. Read u32 `locator` from that mapping.
3. Map 0x110000 bytes starting at
   `((locator << 12) + 0x80000600) & 0xffffffff`.
4. Unmap the four-byte locator, parse the ROM, unmap the ROM, clear the stored
   pointer, and return the parser's status.

This is an observed Windows fallback, **not yet a validated Linux acquisition
procedure**. In the current machine's privileged `/proc/iomem` snapshot,
`0x1182bc80` has no named resource owner. The base produced by a hypothetical
zero locator (`0x80000600`) lies in a reserved range, but the full hypothetical
window would cross System RAM. No actual locator was read, and no physical
window was mapped. Zero must not be substituted for the unknown locator.
Register identity, accessibility and the resulting range need confirmation
before implementing a physical read path; strict `/dev/mem` protections remain
enabled and were not bypassed.

## Registry override timing

`scripts/test_vbios_startup_gate.py` executes the original global initializer
`0xd38a88`, settings constructor `0x195d6d8`, policy initializer `0xd440d0`,
gating-mask producer and start wrappers in one emulated instance. The actual
profile/HAL callback constructors and HAL default block supply the callbacks
and initial `HAL+0x490=1`. Outer allocation/resource discovery is reconstructed;
ROM acquisition supplies synthetic decoded fields, and remapper, buffer
setup, HFRP, power, loader and post-start operations are recording stubs.

For both registry-supplied ROM and physical-fallback branches, acquisition
occurs **before** the `RmSocDlaEnableVbios` override is queried. The override
disables later consumers such as tuning, but the policy initializer still
uses an existing settings object for gating bytes and feature updates. A test
with override zero changes the resulting mask from `7` to `0x777` solely by
changing synthetic VBIOS frequency/gating flags. Feature bit 0 rejects the
global initialization with `0x40` before the override is queried at all.

Both acquisition boundaries were also made to return error `0x65` without
populating settings. Windows ignores that return, retains the constructor's
zeroed settings, applies its 4/1/3 substitutions and reaches the loader stub.
The final global status is zero. This is an error-propagation defect, not proof
that empty settings are valid or that a physical NPU can boot without them.
Linux must retain explicit policy validation and propagate acquisition errors.

A counterfactual control sets the field to zero **before** entering the global
initializer. That does skip settings allocation and acquisition, confirming
the gate itself. It does not establish a production path that applies the
registry override that early, or justify silently changing Linux's board-policy
requirements. Fifteen cases cover the two acquisition sources, five override
values, retained policy effects, early rejection, the counterfactual gate and
both ignored acquisition errors. No physical registers are accessed.

Static instruction/import inspection further identifies the value reader's
OS boundary: `0xd3b6b0` uses `ZwOpenKey` (IAT `0xe3e480`) and
`ZwQueryValueKey` (`0xe3e488`), with ANSI-to-Unicode conversion through
`RtlInitAnsiString`/`RtlAnsiStringToUnicodeString`. The key name comes from
`outer+0x158`; this is not an ACPI `vbios` property lookup. The fallback mapper
`0xd3b4f8` tail-calls `MmMapIoSpace` (IAT `0xe3ead8`) with the supplied physical
address and size. This confirms the physical nature of that fallback without
establishing that its locator register is safe to access on Linux.

```sh
venv/bin/python scripts/test_vbios_startup_gate.py
```

## Still required for hardware bring-up

The local GPU driver's `/proc/driver/nvidia/gpus/000f:01:00.0/information`
reports VBIOS **9a.0b.31.00.04**. This is a version string, not the ROM contents,
and does not prove that it contains the NPU's `l` table. The examined ACPI DLA0
description supplies no VBIOS buffer. No matching raw VBIOS has been extracted.

Read-only inventory on 2026-10-25 established the system-firmware ESRT class
`97686c57-e102-5ecf-ae23-27647f24f0ca`, version 768 (`0x300`), type 1. This is
system firmware identity, not VBIOS content. The graphics debugfs search found
no file named for BIOS. Simple ASCII/UTF-16 string searches of the local ASUS
WinRE SYSTEM hive found no `vbios` or `NVDA200A` entries; this is not a complete
registry-hive audit or evidence that a running Windows installation lacks them.

The [ASUS model support page](https://www.asus.com/supportonly/h7607ba/helpdesk_bios/)
exposed a BIOS API request whose response was `FAIL` with no result. A
[Microsoft Catalog query for the firmware class](https://www.catalog.update.microsoft.com/Search.aspx?q=97686c57-e102-5ecf-ae23-27647f24f0ca)
reported no results. These narrow lookups did not yield an artifact; they do not
establish that no matching firmware package exists. Raw acquisition metadata
and HTTP responses are in ignored `results/` files.

The next acquisition should use a matching vendor firmware artifact or a
reviewed read-only path to an existing firmware copy. The actual table values,
override policy and remaining cold-start ordering are still required before
integrating this policy with the physical HFRP/SROOT startup. Synthetic test
values and another machine's firmware must not become board defaults.

## Native gating-policy preparation

`n1x_dla_prepare_gating()` now derives the normal HAL-1 gating policy from a
fully decoded VBIOS policy. It produces the request-permission mask, the nine
outer policy bytes consumed by CONFIG/power handling, the SLCG override flag
and the four HAL feature bytes at offsets 0x47d/0x47e/0x484/0x485. It implements
zero PPS substitutions and the original low-to-high feature update order,
including bit 20's effect on both power and rail gating. It does not mutate
its input or perform I/O. Partial three-table decoder results and feature bit
0 (rejected by the Windows global initializer) are rejected with unchanged
output. Reserved feature bits have no effect, matching the pinned updater.

This API represents **normal defaults without registry overrides**. It does
not implement pre-silicon owner mode, allocations, remapper installation,
CONFIG exchange, or firmware loading. A nonzero request mask is permission to
request an operation, not a measurement of physical power state. The caller
still needs matching board provenance and the remaining startup sequence.

`test_hfrp_policy_init.py` compares every output with execution of the complete
Windows policy initializer and mask producer in **130 cases**: individual
feature bits, conflicting low/high bits, random combined masks, zero/nonzero
source flags and boundary PPS bytes. Five rejected-input cases preserve output
and input. Normal and UBSan runs pass; the component builds against the running
kernel. It remains outside the live module lists.

## Additional acquisition checks

The ASUS support page's `CheckModelExists` result identifies product ID
**32883**; its BIOS request had instead used placeholder ID 99999. Requests to
[GetPDBIOS with ID 32883](https://www.asus.com/support/webapi/ProductV2/GetPDBIOS?website=global&model=H7607BA&pdhashedid=&pdid=32883&cpu=&siteID=www&sitelang=)
on 2026-10-25 still returned `FAIL`/null for global uppercase/lowercase model
names and Japanese uppercase. No BIOS artifact was obtained. Raw responses
are retained under ignored `results/asus-bios-32883-*.json`.

The installed NVIDIA 615.78.08 source's `ctrl2080bios.h` GET_INFO/GET_INFO_V2
interfaces expose revision/OEM revision, not the raw ROM. The Linux procfs
information file prints a cached version string. Its public
`kgspExtractVbiosFromRom` dispatch chooses the direct-ROM reader only for the
listed Turing/Ampere/Ada families, not a demonstrated N1X path. These checks
do not prove that all possible GPU APIs lack a dump mechanism.

A Capstone scan of executable sections in the pinned Windows KMD and the
extracted nvpep/nvSCP/nvsrootmbx drivers found no second confirmed reader of
`0x1182bc80`. The inspected partial-constant matches were logging IDs,
unrelated offsets or allocation flags. Ghidra exports are in ignored
`results/kmd-ghidra-vbios-locator-neighbors.txt`. This does not identify the
register or establish access safety; no physical locator read or arbitrary
ROM mapping was attempted. The known address remains a reverse-engineering
lead rather than an implemented Linux acquisition path.

## GSP diagnostic export investigation

The installed Linux GPU stack has a diagnostic labelled `VBIOS`, but its
traced producer supplies **MXM data**, not a demonstrated full board ROM.
It does not resolve NPU policy acquisition. This is a **different binary**
from the Windows NPU KMD: NVIDIA 615.78.08
`gsp_ga10x.bin`, SHA-256
`a20100cafd5a85e22b3e13188339730c6ced94bd5f654984dd2ed274ed58d584`.
Its `.fwimage` contains an RV64 ELF at payload offset `0x1dd000`, SHA-256
`eb092d6d6f156e1403b479bcdc33c6f0d7470ad20f259b9c97ff3e06ef80eb5c`.
The addresses below are virtual addresses in that ELF, not Windows RVAs.

Function `0x1bd0148` conditionally exports a buffer:

| Input or operation | Observed contract |
| --- | --- |
| First argument + `0x72e` | Nonzero byte required; the traced MXM producer sets it after accepting descriptors |
| Second argument + `0x630` / `0x638` | Nonnull byte-buffer pointer / u32 length |
| Encoder initialization `0x1058a44` | Allocates input length + 32; outer field descriptor at `0x1c4e8f8` |
| Encoding `0x10580b4` | Byte field descriptor at `0x1c4a770`; copies the full input buffer |
| Finalization `0x1058984` | Allocates a 24-byte output-list node and appends it to third argument + 8 |
| Publisher `0x1c1b784` | Receives a record named `VBIOS`, type 2, referencing the encoded bytes |

The descriptors match the public `prbrt.h` representation: outer optional
message field **307**, containing optional bytes field **1**. The wire format
is `field(307, field(1, original_bytes))`. This establishes byte preservation,
not the meaning or provenance of those bytes.
The publisher is separately identified as the GSP NOCAT path: its RPC event
is `0x1020`, and its record layout matches the host's
`_kgspRpcGspPostNocatRecord()` in `kernel_gsp.c`. That RPC record has a bounded
1024-byte diagnostic slot; it must not be confused with the complete output
list retained by the exporter. The host's platform flags enable GSP NOCAT for
Windows builds. None of this proves that Linux's normal dump command reaches
the exporter or returns its complete output list.

`scripts/test_gsp_vbios_export.py` executes the original exporter **and the
original protobuf encoder**. Only allocation/free, logging and the NOCAT
publisher are stubbed. It compares exact encoded bytes for seven synthetic
buffer sizes from zero through `0x110000`, and tests the disabled gate, null
buffer and both allocation failures. Failure of the output-node allocation
silently discards the encoded buffer and publishes an empty descriptor. Thus
neither a publisher call nor a successful-looking return establishes capture.
All 11 exporter cases pass. The 0x110000-byte case checks the exporter with
an injected pointer; it is **not** evidence that its producer creates a ROM
buffer of that size. No synthetic bytes are valid board policy.

### MXM producer, rather than a ROM acquisition path

Ghidra and radare2 trace the copy into the export object's `+0x630/+0x638` to
function `0x1180988`. Its registry reader receives `MXMDisable`; the property
reader receives `MXMVersion`, `MXMSize` and `MXMData`. On the alternative
platform-reader path, it obtains an eight-byte header, reads the version at
header byte 4 and calculates total size as `u16(header+6) + 8`. It rejects
sizes above **4096** before requesting the body. It then requires version 3,
checks the byte-sum checksum and parses MXM descriptors. These observations
are consistent with the distinct MXM system-information structure in the
[Linux Nouveau MXM implementation](https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux/+/8934827db5403eae57d4537114a9ff88b0a8460f/drivers/gpu/drm/nouveau/nvkm/subdev/mxm/base.c),
not an identification of this P16's firmware storage.

After descriptor acceptance, the original tail at `0x1181696` sets the gate
at first argument + `0x72e`. If the cached pointer is null, it allocates the
MXM input length, copies from the temporary MXM buffer into `+0x630`, stores
the length at `+0x638` and releases the temporary buffer. An existing cached
pointer is retained. Setting `MXMDisable` nonzero clears the gate. No option
has been changed on this machine.

The same test script now executes the original producer **prefix**, with
registry/property/platform reads and allocation/logging boundaries stubbed.
Six additional cases exercise both input sources, the disable flag, bad
checksum, accepted sizes 16 and 4096, and rejection at 4097 before the body
read. Successful cases stop at `0x1180f0a`, before descriptor decoding; they
do not claim that a complete MXM structure was accepted or cached. The
copy-tail identification is static evidence. Ghidra's decompilation has an
unresolved descriptor jump table, so it is not used as proof of every parser
branch. The export is in ignored `results/gsp-vbios-producer.txt`.

This corrects the earlier acquisition inference: a `VBIOS` label and faithful
buffer export are insufficient to identify the actual BIT `l` ROM required
by the Windows NPU startup path. The MXM path is not a substitute for that ROM.

```sh
venv/bin/python scripts/test_gsp_vbios_export.py \
  --elf /path/to/locally-extracted-gsp-rm.elf
```

The file must match the pinned ELF hash. Local extraction currently lives in
`../src/firmware/extract_gsp.py`; its manifest is
`../results/firmware/extraction.json`. No firmware binary is committed.

The public Linux source registers `NVDUMP_COMPONENT_ENG_VBIOS` (component 9)
through a GSP dump callback. `NV2080_CTRL_CMD_NVD_GET_DUMP_SIZE` (`0x20802401`)
is nonprivileged; `NV2080_CTRL_CMD_NVD_GET_DUMP` (`0x20802402`) requires an
administrator. A real component-9 size query on this P16, kernel
`7.2.8-23-omarchy-p16-asus-npu1`, driver 615.78.08, returned `NV_OK` and **250
bytes** on 2026-10-25. This proves an accessible diagnostic size query, not
raw ROM acquisition. The small result does not establish a full board ROM;
the contents and the relationship to the separate field-307 exporter remain
unverified. The local query source and result are retained in ignored
`results/nvd-vbios-size.cpp` and `results/nvd-vbios-size.txt`.

An unprivileged `nvidia-debugdump --dumpall` returned permission errors despite
process exit status zero and wrote only a 44-byte empty archive. A usable dump
must be checked for actual members and payload bytes. No matching board ROM
has yet been recovered through this route, and no Linux NPU startup policy
has been populated from it.

## GSP ROM source: GFWI container

A fresh, read-only `NV2080_CTRL_CMD_MC_GET_ARCH_INFO` query on this machine
returned architecture `0x1b0`, implementation `0x0b`, revision `0xa1` and
subrevision 0. The installed driver's chip mapping identifies the `0x1b/0x0b`
family as **GB20B**, HAL index 70. GB10B uses index 61 and must not be substituted
solely because this is an N1X/Spark system.

The pinned RV64 ELF's NVOC class descriptor at `0x1cb1130` has class ID
`0x5dc772` (OBJVBIOS, matching the public GPU header), size `0x1700` and
constructor `0x1a0e5cc`. Executing that constructor with HAL index 70 selects:

| Object slot | Target VA | Observed operation |
| --- | --- | --- |
| `0x150` | `0x11b8424` | Returns ROM buffer capacity `0x700000` |
| `0x240` | `0x11bcdc4` | Obtains a GFWI container from one of two existing GPU objects and extracts its ROM member |

`scripts/test_gsp_rom_source.py` executes the original constructor with only
NVOC dynamic casts stubbed. Indices 61/62/70/71 and RM variants 0..3 all select
the same two targets (16 cases). This confirms the callback for the observed
GB20B index; it does not identify which source branch is active on this boot.

The reader first checks `GPU+0x22d0` (`KernelFsp`), its flag `+0x1e9` and
descriptor pointer `+0x1f0`. When the object and flag are present, it copies
that descriptor's memory buffer into a temporary allocation before calling
`0x11bcd14`. Only an absent object or clear flag selects the other object at
`GPU+0x22f0`: with flag `+0x378` set, its buffer at `+0x3500` and size at
`+0x3508` go to the same extraction helper. An enabled first source with a
null descriptor, zero size or bad signature **does not fall back**. These are
GSP virtual object offsets, **not CPU physical addresses**.

The GFWI member layout is:

| Byte offset | Type | Meaning established by the original copy helper |
| --- | --- | --- |
| `0x00` | u32 LE | `0x49574647`, bytes `GFWI` |
| `0x20` | u32 LE | ROM offset relative to container start |
| `0x24` | u32 LE | ROM byte count |

The original helper compares offset and size individually with the container
length and compares size with output capacity. It **does not check their sum**.
An offline case with offset 120, size 32 and declared container length 128
returns success and copies bytes beyond that declared extent (the emulator
deliberately provides surrounding synthetic memory). Do not reuse its bounds
logic for file parsing. Zero-length and overlapping-header inputs also pass
the original helper and are rejected by the local extractor.

`scripts/extract_gfwi_vbios.py` extracts from an **already acquired file**.
It requires the complete header, a non-overlapping member, nonzero size within
the observed HAL capacity, and a fully contained offset-plus-size range. It
creates a new output file exclusively and performs no hardware access. Twelve
original-helper cases test copying, error preservation and the stricter file
decoder; all 40 truncated-header lengths are rejected. This produces raw ROM
bytes only: board identity and the presence of the NPU's complete BIT `l`
policy must still be established separately.

```sh
venv/bin/python scripts/test_gsp_rom_source.py --elf /path/to/gsp-rm.elf
# Only after acquiring a matching container:
venv/bin/python scripts/extract_gfwi_vbios.py board.gfwi board.rom
```

The separate global pair `0x41ae600` (address) / `0x41ae5f8` (size), read by
the ROM initialization path at `0x1ac9304`, is **an override input**. Its writer
at `0x1ad1188` copies offsets `0xa8/0xb0` from the `GSP ` boot-arguments block.
Those offsets correspond to `GSP_ARGUMENTS_CACHED.vbiosOverrideArgs.pa/size`
in the public `gsp_init_args.h`. It is not evidence for an independently
readable board ROM address and was not populated or modified by this work.

No valid raw GFWI container was found in the four inspected local files:
`gsp_ga10x.bin`, `ucodes_ga10x.bin`, the DGX Spark `socfw.cap`, and the pinned
Windows `nvlddmkm.sys`. This literal-signature scan does not rule out compressed
or otherwise wrapped content. Acquisition of the actual source buffer remains
open. No GPU object, NPU register, boot configuration or firmware was changed.

### KernelFsp source is disabled by the GB20B constructor

The class-lookup fragment at `0x140d55c..0x140d570` maps NVOC ID `0x87fb96`
to `GPU+0x22d0`. That ID is `KernelFsp` in the installed driver's generated
`g_kern_fsp_nvoc.h`. The pinned firmware's class record at `0x1ca8400`
independently gives ID `0x87fb96`, object size `0x300` and constructor
`0x19b3f88`. These identifications are more specific than matching an FSP
string elsewhere in the image.

Ghidra decompilation and execution of that original constructor agree:
for this machine's HAL index **70**, all four RM variants set the inherited
missing property at `+0x58` to 1. The source flag at `+0x1e9` and descriptor
at `+0x1f0` remain zero. The boot-command preparation slot at `+0x150` points
to `0x19aa23c`, an original stub returning success without preparing data.
The public `g_kern_fsp_nvoc.c` identifies the matching constructor decision
as `PDB_PROP_KFSP_IS_MISSING`: indices in group 2 must match mask `0x303f`;
index 70 has bit 6, which is absent. This is a software selection property,
not proof that the hardware lacks all FSP-related circuitry.

The extended `test_gsp_rom_source.py` executes 24 original constructor cases:
indices 61/62/70/71 are marked missing; 64/65 are positive controls with the
property clear and a different preparation callback. Twelve additional
full-reader cases distinguish the two sources with different synthetic ROM
payloads. They verify source precedence, fallback only when the first source
is disabled or absent, temporary allocation/free, unchanged outputs on error,
and the class-lookup fragment. Allocation/free and logging are stubs; both
copy operations execute original instructions. Descriptor length is at
`+0x30`, its tested field at `+0x8c` is 0 or 1, and its buffer pointer is at
`+0x110`. Cases entering the assertion path for field values above 1 are
not part of this replay.

The public Hopper implementation prepares a sysmem FRTS descriptor named
`pSysmemFrtsMemdesc`, but that alone does **not** identify the GSP object's
`+0x1f0` member or establish a live host buffer. Its allocation procedure
must not be imported into the NPU driver on the strength of that name.
For GB20B, the constructor rules out this first source in the normal
initialized state. Acquiring the FRTS startup buffer below remains the
relevant route. A live snapshot has not yet confirmed the object state or
provided board ROM bytes.

### FRTS supplies the GFWI startup buffer

The platform bases themselves come from a separate **488-byte startup
parameter block**, not from the GFWI container. The pinned init ELF enters
at `0x100283e` and jumps to `0x1006d16`. When entry argument 4 is zero,
that function maps 488 bytes at the address supplied in argument 5 through
`0x1002412`, copies them with the original `0x1002b26` into `0x4002368`,
then unmaps the temporary mapping. Argument numbering here is one-based
(`a0` is argument 1); these are GSP task arguments, not a Linux syscall.

| Startup-block offset | Copied global | Observed use |
| --- | --- | --- |
| `0x48` | `0x40023b0` | Base selected when the selector is zero |
| `0x58` | `0x40023c0` | Base selected when the selector is nonzero |
| `0x1a0` | `0x4002508` | WPR metadata offset added to the selected base |
| `0x1e0` | `0x4002548` | Byte selecting the base |

Three added `test_gsp_frts_handoff.py` cases execute the original ELF entry,
startup-block copy and base selector with synthetic, unaligned input. Mapping
and unmapping are stubbed. A mapping failure reaches the fatal-error boundary
before any block copy. Successful cases stop before the next mapping setup;
they do not bootstrap the complete task. The alternative nonzero-argument-4
path receives a same-sized block through IPC and has only been inspected in
assembly, not replayed here. Ghidra truncates that path at the LibOS trap,
so its incomplete C output is not evidence that the IPC path is absent.

The producer of this startup block and its live values remain unidentified.
In particular, the selector has not been assigned a hardware-type name, and
neither base has been established as a CPU physical address. Reading the host's
cached FRTS offset alone must not be turned into an arbitrary physical-memory
read by assuming a zero base.

The packaged init-task ELF `embedded-8-1d1000.elf`, extracted from the same
615.78.08 GSP payload, has SHA256
`46b99be784a02ec8221b25d6cadaa6bf03eeadd45f5234de286eefa78213f028`.
Its startup path explains how the RM task receives the container. Addresses
in this subsection belong to that init ELF unless explicitly labelled RM.

`0x1002514` maps a 256-byte WPR metadata structure at the platform base plus
the offset in `0x4002508`. Before using it, the function checks magic
`0xdc3aae21371a60b3`, revision 1 and the verified marker
`0xa0a0a0a0a0a0a0a0` at byte `0xf8`. Its remaining region-validation calls
have not been replayed. The mapped metadata pointer is stored at `0x40025b0`.

In the startup function `0x100496c`, the block `0x10052be..0x100530e` reads
two u64 fields from that metadata: `+0x98` and `+0xa0`. These exactly match
`GspFwWprMeta.frtsOffset` and `.frtsSize` in the installed public
`gsp/gsp_fw_wpr_meta.h`; a C compile with `sizeof`/`offsetof` assertions
confirmed 256 / `0x98` / `0xa0`. The next operation, when size is nonzero, is:

```text
map(target_address_space, &startup.frts_va,
    platform_base + metadata.frtsOffset, metadata.frtsSize, flags=4)
```

The map wrapper at `0x10024ec` calls `0x1002412`, which rounds the physical
range to 4 KiB pages and returns the mapping plus its original page offset.
The base selector at `0x1001106` chooses global `0x40023b0` when flag
`0x4002548` is zero, otherwise `0x40023c0`. These are firmware-side addresses
and mapping semantics, not a demonstrated Linux CPU physical-memory mapping.

The init task's outgoing block starts at `0x40020d8` and is 656 bytes
(`0x290`). The send call is at `0x10058cc`. Its FRTS fields are:

| Block offset | Init-task global | RM-task global | Meaning |
| --- | --- | --- | --- |
| `0x118` | `0x40021f0` | `0x4194928` | Mapped container address |
| `0x120` | `0x40021f8` | `0x4194930` | FRTS offset from WPR metadata |
| `0x128` | `0x4002200` | `0x4194938` | FRTS byte count |

The RM entry at `0x1afab76` receives this block into `0x4194810` using its
fifth entry argument as the receive handle. The original wrapper at
`0x1b9fff0` issues a LibOS illegal-instruction trap (`0x0000` at `0x1ba0012`)
with `t0=1`, `a0=3`, `a1=handle`, `a2=destination`, `a3=656`, `a4=1` and
`a5=-1`. This is a firmware task interface, not a Linux ioctl.

The RM block at `0x1ac9518` copies the received address and byte count into
the previously identified GPU object at `+0x3500/+0x3508` and enables its
`+0x378` flag. The actual GB20B ROM reader `0x11bcdc4` can then extract the
GFWI ROM member. This establishes a source chain for that branch:

```text
WPR metadata FRTS range -> init-task mapping -> 656-byte task message
  -> RM startup globals -> GPU object -> GFWI member -> ROM bytes
```

`scripts/test_gsp_frts_handoff.py` replays the original base selector (three
cases), FRTS setup fragment (five cases, including zero size and map failure),
RM receive wrapper, GPU-object installation block, and actual ROM HAL reader.
Three successful synthetic cases verify byte-exact copying through that
chain, including an unaligned FRTS offset. Mapping and logging are stubbed;
the receive trap is replaced with a synthetic message transfer. The test
does **not** execute the complete init task, secure boot, the sending syscall,
or physical memory reads, and it does not prove which source branch runs on
this machine. No vendor binary is modified or included in Git.

```sh
venv/bin/python scripts/test_gsp_frts_handoff.py \
  --init-elf /path/to/embedded-8-1d1000.elf \
  --rm-elf /path/to/embedded-9-1dd000.elf
```

The installed host source also decodes `NVGMC_SC_WPR_LAYOUT_FRTS_OFFSET`
into `GspStaticConfigInfo.fwWprLayoutOffset.frtsOffset` in
`_kgspStaticInfoKeyHandler` (`kernel_gsp.c`). This is a concrete next place
to investigate the running firmware's offset. It does not expose the bytes
by itself. `kgspPopulateWprMeta_GB10B` explicitly says the secure ACR code
fills actual offsets on this integrated-GPU family; initial host allocation
values must not be mistaken for the final firmware layout.

Still needed: the running machine's final FRTS location and contents, or
another verified acquisition of its matching ROM, followed by validation
of its NPU BIT `l` tables. No valid board GFWI container has been acquired.

### Reading the host's cached FRTS metadata

The 615.78.08 host source confirms the return path: `kgspSendInitRpcs_IMPL`
receives a key/value response to `GMCAPI_CMD_GSP_INIT` and decodes key
`NVGMC_SC_WPR_LAYOUT_FRTS_OFFSET` (`0x1081`). The original encoded response is
freed, but the decoded offset remains in `KernelGsp.gspStaticInfo`. On the
ACR-layout path, `kgspPopulateSrRegionsInfo_GH100` copies it into the suspend
metadata. **Do not resend the initialization command to query this value.**

The public `NV2080_CTRL_CMD_FB_GET_FB_REGION_INFO` getter is not a substitute:
its host implementation explicitly excludes reserved and internal heap
regions. The installed module contains DWARF for parts of the kernel layer,
but GDB finds no type information for `OBJGPU`, `GspStaticConfigInfo`, or the
`g_pSys` variable. The relevant compiled accessors nevertheless retain names
and can be disassembled directly.

`scripts/read_gsp_frts_cache.py` follows those compiled accessors' field
offsets through kernel RAM. It opens `/proc/kcore` read-only, resolves only
four named NVIDIA symbols from `/proc/kallsyms`, and reads the pointer chain
and cached offset/size. It does not call kernel functions, send GPU RPCs,
read `/dev/mem`, map GPU memory, access MMIO, load a module, or upload firmware.
The output remains a **firmware-region offset**, not a CPU physical address
or a ROM dump.

This helper is intentionally restricted to the existing build:

| Guard | Pinned value |
| --- | --- |
| Kernel | `7.2.8-23-omarchy-p16-asus-npu1` |
| NVIDIA version | `615.78.08` |
| Module srcversion | `4200B1C66D4D12038311105` |
| Installed `nvidia.ko` SHA256 | `d8878935a9c353dc82cc7b31f85a8a1e9eac6d0dab624a03451fc3616f56da1c` |

Before walking objects, it compares selected non-relocated instructions in
the **running** `gpumgrGetGpu`, `gpuGetGspStaticInfo`, and
`kgspPopulateSrRegionsInfo_GH100` against the inspected module. It rejects
unavailable symbols, different code, invalid pointers, multiple GPU handles,
an uninitialized GPU, an alternate static-info path, and a non-ACR layout.
It requires the static and suspend-cache offsets to agree and the host WPR
size to agree with the suspend-cache size. It samples twice and rejects
different results. These are consistency checks, not a lock or a guarantee
against concurrent teardown; use it while the GPU driver remains loaded and
the machine is awake.

```sh
# Offline bounds/instruction tests; uses pyelftools from requirements.txt:
venv/bin/python scripts/test_gsp_frts_cache.py -v
# Root, read-only inspection of this specific running build:
sudo /usr/bin/python scripts/read_gsp_frts_cache.py
```

Eight offline tests pass, covering segment boundaries, overlapping segments,
short reads, invalid headers/pointers, changed-code rejection before cache
access, matching instruction bytes in the installed module, and synthetic
cache agreement/disagreement, including a consistent zero host-requested size.
The reader was successfully executed with root privilege on 2026-10-26;
see the live results below. It has not acquired the container itself.

### Live acquisition results, 2026-10-26

The privileged cached-metadata reader passed its module/instruction guards
and both snapshots agreed. It returned GPU instance 0, firmware-reported
FRTS offset **`0xb9500000`**, and host/suspend-cache FRTS size **0**. The raw
record is retained locally in `results/gsp-frts-cache-live-20261026.json`.

The zero size is expected from the host HAL: generated
`g_kernel_gsp_nvoc.c` selects `kgspGetFrtsSize_b2b553` for GB20B; that inline
function in `g_kernel_gsp_nvoc.h` returns zero. The host WPR input and its
suspend copy therefore do not supply the final firmware-side extent.
The helper now labels this value **`host_requested_frts_size`**, rather than
the ambiguous `frts_size`. Zero neither proves an absent ROM nor permits a
guessed read length. The privileged `/proc/iomem` snapshot has no resource
covering `0xb9500000`: the preceding listed region ends at `0xafffffff`,
and the next starts at `0xb9600000`. That gap is not evidence of CPU access
permission or of a zero firmware base. No physical read was attempted.

The previously pending `nvidia-debugdump` completed successfully. Its archive
SHA256 is `7dab37a21f28658c402f246fb7d441123450ea52269c7debd34eefa0b9f1e4de`;
ZIP CRC validation passes. Six members are present: `system_info.pb` (42230
bytes), `error_data.pb` (1091), `nvlog.log` (697084), `debug_buffers_00.pb` (2),
`rm_00.pb` (54799), and `nvlog.gpu000.log` (697084). None contains a literal
GFWI or BIT signature. The two logs are binary NvLog data, not decoded text;
this check does not exclude information encoded in them. The archive remains
local because its system-information records contain process/client data.

`scripts/read_nvidia_vbios_dump.cpp` adds a targeted, read-only component-9
diagnostic request using the matching public SDK. It queries the size and
retrieves one dump; it does not reset hardware, change diagnostic settings,
map physical memory or upload firmware. The live size estimate was 250 bytes.
A buffer of exactly that size was rejected with `NV_ERR_BUFFER_TOO_SMALL`;
a bounded 1 MiB buffer succeeded and returned **250 bytes**, saved locally as
`results/nvd-vbios-component9.pb`, SHA256
`12011d038889aa5a4dff3fe2a583513383797c6b12fbaefe53527f5b34798788`.
The program creates its output exclusively and rejects failed/empty replies.

That protobuf contains root field 3, with nested fields 1 (27 bytes), 101
(89 bytes), and **111 (123 bytes)**. Field 111 contains version `0x9a0b3100`
and OEM revision 4, consistent with `9a.0b.31.00.04`. Its raw-ROM field 10
is absent. The returned bytes are diagnostic metadata, not the required ROM.

The corresponding original GSP exporter is `0x11bd55c`, identified by its
use of field-111 descriptor `0x1c4d848`. Ghidra and instruction inspection
show a conditional bytes-field-10 descriptor at `0x1c4cac8`: the candidate
ROM pointer/length are `OBJVBIOS+0x2a8/+0x2b0`. Copying is gated by pointer
presence and available encoder space, divided by 4 or 2 according to the
object flag at `+0xfa8`. A separate table-copy branch needs more than
65536 bytes of encoder space. This is static evidence, not a replay of the
complete exporter or a measurement of the live pointer/length.

The host `rpcDumpProtobufComponent_v18_12` in `kernel/vgpu/rpc.c` caps the
GSP output to `min(maxRpcSize - headers, encoder_space)`. The normal GSP queue
initializer sets its maximum element to 16 RM pages (64 KiB), from which
queue/RPC headers are subtracted. Thus a larger userspace buffer does not
remove this per-message limit and cannot reach the >64 KiB table branch.
The current dump does not distinguish a null ROM pointer from a size/budget
rejection. No queue limits or driver memory were patched.

```sh
c++ -std=c++17 -Wall -Wextra \
  -I/usr/src/nvidia-615.78.08/src/nvidia/arch/nvalloc/unix/include \
  -I/usr/src/nvidia-615.78.08/src/common/sdk/nvidia/inc \
  scripts/read_nvidia_vbios_dump.cpp -o results/read_nvidia_vbios_dump
sudo results/read_nvidia_vbios_dump results/component9-new.pb
```

Still needed: the final firmware extent and verified CPU mapping, or a
supported export of the cached ROM. Root authentication is no longer the
observed blocker for these completed captures. No NPU upload or restart was
performed, and physical NPU firmware boot remains unverified.

### Scratch candidate and early RAM capture

**Physical test status, 2026-10-26:** the first capture UKI hung with a black
screen immediately after menu selection. The machine recovered by booting the
original npu1 Image. Neither the persistent journal nor pstore contains that
failed boot. The root cause is unresolved; successful builds, symbol CRCs and
mocked guard tests did not establish physical bootability. A capture-OFF
diagnostic is prepared below to replace the failed menu entry.

The component-9 capture contains another useful field: VBIOS field 12 is a
register dump, not another ROM. The original GB20B-selected callback
`0x11c1ac8` calls the register encoder at `0x1b9f8f0`. Its loop reads **64
32-bit words**, at four-byte strides, from a base returned by the bus device
descriptor plus `0x400`. The live protobuf reports base `0x1400`, space 1,
and field 4 contains those words as packed varints. The public GB20B
`dev_bus.h` identifies this range as `NV_PBUS_SW_SCRATCH`.

`scripts/decode_vbios_scratch.py` decodes this exact message shape offline.
It rejects duplicate required fields, invalid register counts, wrong bases,
truncated messages and integer overflow. Unknown configuration bits remain
visible. It performs no memory access or address translation.

The live final three words are:

| Scratch index | Register | Value |
| --- | --- | --- |
| 61 | `0x14f4` | `0xfc000000` |
| 62 | `0x14f8` | `0x00000000` |
| 63 | `0x14fc` | `0x00010009` |

NVIDIA's published GB100 `dev_bus_addendum.h` assigns these to the insecure
FRTS low/high address and configuration. Under that layout, these values
describe **sysmem at `0xfc000000`, length `0x9000` (36 KiB)**. This is an
inferred GB20B interpretation, not proof that the GPU address is a CPU physical
address. GPU IOMMU group 23 uses a DMA domain; this address is outside its
reported direct-mapping reservations. No GPU address was blindly mapped.

The independently checked CPU-address candidate lies in Linux System RAM,
within its reserved `0xfbd00000..0xffcfffff` range. A bounded 40-byte read
through the matching `/proc/kcore` physical PT_LOAD returned zeros, SHA256
`2c34ce1df23b838c5abf2a7f6437cca3d3067ed509ff25f11df6b11b582b51eb`.
No full container was acquired. The nine `/proc/kpageflags` entries are
`0x100000000` (reserved), and memblock marks the encompassing memory as
ordinary mapped RAM (`NONE`), rather than `NOMAP`.

The boot journal identifies the reservation precisely:

```text
software IO TLB: mapped [mem 0x00000000fbd00000-0x00000000ffd00000] (64MB)
```

The matching kernel's `kernel/dma/swiotlb.c`, function
`swiotlb_init_io_tlb_pool`, clears the entire pool with `memset(vaddr, 0, bytes)`.
Therefore any earlier firmware data at this **CPU physical address** would
have been destroyed during initialization. The observed overlap and clearing
are established; the prior presence of the desired FRTS container is not.
Further scanning of this live DMA pool would not recover the pre-boot data.
For comparison, upstream [SWIOTLB source](https://github.com/torvalds/linux/blob/master/kernel/dma/swiotlb.c)
contains the same clearing operation. [`kcore.c`](https://github.com/torvalds/linux/blob/master/fs/proc/kcore.c)
also has zero-fill paths, so a zero-only kcore read by itself is insufficient
to establish the contents of arbitrary reserved memory.

#### Capture implementation

`driver/n1x_frts_snapshot.c` is built into a **separate test Image**, not into
the observation/bootstrap modules. The integration patch calls it immediately
after `arm64_memblock_init()`, before paging initialization and SWIOTLB.
It is inactive without the exact parameter
`n1x_frts_snapshot=p16-bios300`. DMI discovery runs later on ARM64, so the
build/installer check the exact ASUS product and BIOS; the early function
requires this explicit board-specific parameter instead of claiming an early
DMI check.

The fixed candidate is read only if EFI is active and the whole 36 KiB lies
in one ordinary memblock RAM region with no overlapping reservation. The
function uses a temporary **read-only RAM mapping**. It first reads 40 bytes,
requires `GFWI` and a nonempty, in-bounds ROM member, then copies the fixed
extent into kernel-owned storage and checks stability. Invalid/changing data
is not published. It does not reserve or alter the source memory, bind an
NPU, access device registers, upload firmware or issue a reset.

A successful copy appears as the root-readable, read-only file
`/sys/firmware/n1x_frts_snapshot`. The log prefix is `n1x-frts:`. The snapshot
storage becomes read-only after kernel initialization. A missing file is a
failed acquisition, not permission to retry NPU startup with guessed policy.

Build from the audited npu1 tree and verified UKI staging directory:

```sh
python scripts/test_vbios_scratch.py -v
python scripts/test_frts_snapshot.py
python scripts/build_frts_snapshot.py \
  results/linux-7.2.8-p16-npu1 results/npu1-uki-v3 \
  results/frts-early-v1 --jobs 8
```

The builder uses isolated copies, retains the npu1 kernel release and
configuration, and compares freshly generated `vmlinux.symvers` against the
original built-in exports in `Module.symvers`. The live build matches all
**15,527 exports and CRCs**. Its existing input/display/network modules can
therefore be reused; no installed kernel/module package is replaced.
The rebuilt UKI passes the existing verifier for kernel contents, command
line, ACPI overrides, NVIDIA/input modules, and encrypted-root boot files.
Sixteen mocked C guard cases pass with address/undefined-behavior sanitizers;
four protobuf test groups include every truncated prefix of a valid fixture.
These checks do not establish a successful physical boot or capture.

Build artifacts, retained locally:

| Artifact | SHA256 |
| --- | --- |
| capture Image | `d775641d7001dcf0ffb07fda59ee7ed57dccf6bf65ef44b88d4f1759d62db6d9` |
| capture UKI | `16ac622a8832ef10a9d1a9da9cfbf2d2301591269245bdb325b0fb96bbc619c1` |

`scripts/install_frts_snapshot.py` now rejects the above physically failed UKI
hash. Its original deployment verified the artifacts and existing default
Image hash before adding the manual **N1X FRTS capture** entry. It did not
change the default, timeout, installed npu1 Image/modules or original entries,
and never reboots. Its checkpoint is `/var/lib/n1x-npu-test/frts-capture`.
The following was the original installation command; the failed Image is now
explicitly rejected. Use the diagnostic procedure below instead. Absolute
paths are necessary with `pkexec`, which may change the working directory:

```sh
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/install_frts_snapshot.py \
  /home/johann/n1x-lab/npu/results/frts-early-v1
```

For a future successfully qualified capture boot, check `journalctl -k -b` for
`n1x-frts:` and acquire the sysfs snapshot if present. Run
`scripts/extract_gfwi_vbios.py` on the saved container, then validate board
identity and BIT token `l` using the existing bounded decoder. Do not load or
trigger the NPU bootstrap as part of this capture. To recover from a failed
boot, select the unchanged **Omarchy/linux-p16-asus-23** default entry.

#### Black-screen investigation and capture-OFF diagnostic

The failed boot left no persistent journal boot ID and no files in either
`/sys/fs/pstore` or `/var/lib/systemd/pstore`. The recovered system runs the
original npu1 kernel, build `#2`, without the capture parameter. Its power
profile was restored from `balanced` to the user's requested `performance`.
These observations do not locate the hang within the EFI loader, early
kernel, or physical RAM access.

An additional whole-kernel VM test executes the actual built capture Image
through EDK II, using QEMU 11.1.2's ARM64 `virt` platform, TCG, one `max` CPU
and 4 GiB RAM. A synthetic 36 KiB `GFWI` container is loaded into guest RAM at
`0xfc000000`; a tiny static init reads the resulting sysfs file until EOF and
compares all 36 KiB. It prints **`FRTS_VM_CAPTURE_PASS`**, then powers down.
This tests early mapping, capture, later publication and reading after kernel
initialization. It does not validate the P16's physical address interpretation
or firmware. It boots the kernel Image through EFI, not the complete P16 UKI.
Several KVM/old-EDK-II trials encountered firmware exceptions, including the
known-working base UKI control; those trials do not diagnose the P16 hang.

The VM init source is `tests/frts_vm_init.c`. Local artifacts/logs are
`results/frts-qemu-fixture.gfwi`, `results/frts-vm-initrd`, and
`results/frts-v1-qemu-sysfs-tcg.log`. Firmware comes from Debian's
[`qemu-efi-aarch64_2022.11-6+deb12u2_all.deb`](https://packages.debian.org/bookworm/all/qemu-efi-aarch64/download),
verified against published SHA256
`9a55c7b94fdf13a28928359a77d42e5364aa3ae2e558bd1fd5361955bf479d81`.
It is extracted under `results/qemu-efi`, not installed as host firmware.

```sh
mkdir -p results/frts-vm-root
cc -static -O2 -Wall -Wextra -Werror tests/frts_vm_init.c -o results/frts-vm-root/init
bsdtar --format=newc -cf results/frts-vm-initrd -C results/frts-vm-root init
# Fixture: 0x9000 zero bytes, 'GFWI' at +0, LE32 40/64 at +32/+36,
# and bytes 0..63 at +40. No real firmware or host disk is attached.
timeout 45s qemu-system-aarch64 \
  -machine virt,accel=tcg,gic-version=3 -cpu max -m 4G -smp 1 \
  -nographic -nodefaults -serial mon:stdio -nic none -no-reboot \
  -bios results/qemu-efi/usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -device loader,file=results/frts-qemu-fixture.gfwi,addr=0xfc000000,force-raw=on \
  -kernel results/frts-early-v1/kernel/arch/arm64/boot/Image \
  -initrd results/frts-vm-initrd \
  -append 'console=ttyAMA0 earlycon=pl011,0x9000000 loglevel=7 nokaslr n1x_frts_snapshot=p16-bios300 rdinit=/init panic=0'
```

The initially proposed next physical test was **N1X FRTS diagnostics - capture OFF**.
**It has since been withdrawn after the VM module-BTF failure; see
[boot logging](boot-logging.md).** It used the
same capture-kernel Image and the same initramfs bytes, with only the UKI
command line changed. The capture opt-in is absent. `quiet`, `splash` and
silent log settings are replaced by `earlycon=efifb`, visible messages and
`initcall_debug`. The original root options and quoted ACPI option are preserved.
This is fault isolation, **not a demonstrated fix**. If it boots, the active
capture path becomes the main differentiator; if it still fails, the next
investigation must include the rebuilt Image/EFI path independently of capture.

```sh
venv/bin/python scripts/test_frts_diagnostic.py -v
venv/bin/python scripts/prepare_frts_diagnostic.py \
  results/frts-early-v1 results/frts-diagnostic-v2
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/install_frts_diagnostic.py \
  /home/johann/n1x-lab/npu/results/frts-diagnostic-v2 \
  /home/johann/n1x-lab/npu/results/frts-early-v1
```

The preparer verifies every non-command-line PE section is unchanged, retains
virtual addresses/entry point, updates `.cmdline` VirtualSize (BFD otherwise
retains the old size), and runs the existing boot-content verifier. Diagnostic
UKI SHA256: `6a1a8d8a42832929a1093d1189f714ce1aa5c49bdfa7c9e1898fa3354a8929d2`.
The installer replaces only the old capture entry, retains its EFI file, and
backs up the menu under `/var/lib/n1x-npu-test/frts-diagnostic`. It preserves
the normal default and timeout and never reboots. Physical diagnostic boot
remains pending.

#### Diagnostic console handoff correction

The capture-OFF boot photo (`Downloads/boot.jpg`, retained outside the repo)
ends at about 0.309 seconds with `legacy bootconsole [efifb0] disabled`, after
enabling `tty0` on a `colour dummy device 80x25`. The screen remained unchanged
for five minutes. This is an unsuccessful visible boot, but the last displayed
line does not identify a kernel stall: the output channel was explicitly
disabled at that point. The earlier VHE/EL2 timer warning also occurs in the
successfully booted stock P16 kernel and is not sufficient to explain this
failure.

The diagnostic revision included **`keep_bootcon`** alongside `earlycon=efifb`.
The matching kernel's EFI early-console implementation supports remapping the
framebuffer for this mode. Capture remains disabled. This preserves the early
output channel for further fault isolation; it is not a demonstrated fix for
the failed boot. Kernel and initramfs bytes remain unchanged.

```sh
venv/bin/python scripts/prepare_frts_diagnostic.py \
  results/frts-early-v1 results/frts-diagnostic-keep-console
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/install_frts_diagnostic.py \
  /home/johann/n1x-lab/npu/results/frts-diagnostic-keep-console \
  /home/johann/n1x-lab/npu/results/frts-early-v1 --replace-diagnostic
```

The menu title stays **N1X FRTS diagnostics - capture OFF**. The replacement
uses a new EFI filename ending in `-keep-console.efi`, retaining the previous
file and backing up the menu in
`/var/lib/n1x-npu-test/frts-diagnostic-keep-console`. Tests cover replacement
scope, preservation of surrounding entries/defaults, and rejection of a
replacement without `keep_bootcon`.

Deployment needed space on the 2 GiB EFI partition. The already unreferenced
failed capture Image was copied to
`/var/lib/n1x-npu-test/frts-capture/failed-capture.efi`, SHA256 checked against
the original record, and only then removed from `/boot/EFI/Linux`. Its record
now points to the archive. No working kernel or referenced EFI image was
removed. The keep-console diagnostic UKI SHA256 is
`ddac84d8a05bbb608dfb9dc5e8004482fc9443d6e4fe8289027483b79dd39d04`.
