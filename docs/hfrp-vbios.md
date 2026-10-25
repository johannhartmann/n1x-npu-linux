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
field. Disabling it to avoid acquiring the board policy is not a validated
Linux startup configuration.

The initializer checks for a `vbios` property through `0xd3b6b0`. If present,
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

`n1x_dla_parse_vbios_policy()` now locates those tables in an immutable ROM
window of at most 0x110000 bytes. It follows the observed preceding aligned
`55 aa` ROM-base rule, checks BIT header checksum and entry-array bounds, finds
token `l`, validates its declared length and all three nested table ranges,
then invokes the decoder. Null/overflowing pointers, missing ROM signatures,
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
Another 27 negative cases check ROM-level bounds, absent tokens, checksums,
zero/overflowing pointers, ambiguity, header-flood limits and null arguments.
Both BIT entry widths and a nonzero ROM base are tested through the locator.

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
