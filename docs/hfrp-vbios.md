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

The installed Linux GPU stack offers another acquisition lead. This is a
**different binary** from the Windows NPU KMD: NVIDIA 615.78.08
`gsp_ga10x.bin`, SHA-256
`a20100cafd5a85e22b3e13188339730c6ced94bd5f654984dd2ed274ed58d584`.
Its `.fwimage` contains an RV64 ELF at payload offset `0x1dd000`, SHA-256
`eb092d6d6f156e1403b479bcdc33c6f0d7470ad20f259b9c97ff3e06ef80eb5c`.
The addresses below are virtual addresses in that ELF, not Windows RVAs.

Function `0x1bd0148` conditionally exports a buffer:

| Input or operation | Observed contract |
| --- | --- |
| First argument + `0x72e` | Nonzero byte required; its complete producer semantics remain unresolved |
| Second argument + `0x630` / `0x638` | Nonnull byte-buffer pointer / u32 length |
| Encoder initialization `0x1058a44` | Allocates input length + 32; outer field descriptor at `0x1c4e8f8` |
| Encoding `0x10580b4` | Byte field descriptor at `0x1c4a770`; copies the full input buffer |
| Finalization `0x1058984` | Allocates a 24-byte output-list node and appends it to third argument + 8 |
| Publisher `0x1c1b784` | Receives a record named `VBIOS`, type 2, referencing the encoded bytes |

The descriptors match the public `prbrt.h` representation: outer optional
message field **307**, containing optional bytes field **1**. The wire format
is `field(307, field(1, original_bytes))`; it is not just a version string.
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
All 11 offline cases pass. No synthetic bytes are valid board policy.

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
