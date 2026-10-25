# P16 ACPI power-path audit

Audit date: 2026-10-25. **Live-table identity is now verified.** Read-only
collection obtained DSDT, five SSDTs and IORT; no dynamic SSDTs were present.
All six AML tables match the previously audited candidates byte-for-byte.
The observation module subsequently bound both DLA0 and ROT0 with MMIO reads
disabled; no NPU register access or firmware boot has occurred.

## Sources and identity

The original six tables are readable in
`/home/johann/Projects/p16-hardware-fixes/firmware-original/` (2026-10-20).
The candidate set replaces DSDT and SSDT2 with the existing files from
`acpi-extracted/kernel/firmware/acpi/`. Those two hashes match the pinned output
hashes in `p16rtx_linux/acpi/acpi_builder.py`; all six ACPI checksums pass.
Live collection verified full byte identity, lengths and checksums. Raw tables
remain in ignored `sources/`. The live IORT is 6160 bytes, revision 6, SHA-256
`25d88a0a0c881914c347a5cf25c97a2a5f8ed0842af60a871795604a102858bf`.

| Table | Bytes | Candidate SHA-256 |
|---|---:|---|
| DSDT | 74052 | `80e0ad4a051704003d09029cb600a9814e1cd370ce774b9749dc09d2232b5aaf` |
| SSDT1 | 163 | `073904562b87807d1b5504bda4efd43f7665c5fe0886553ff940febd489406cf` |
| SSDT2 | 34465 | `9b28ffc14c19f8b7ea2278de88ff9ec070f5f70401107d81fef067a916aa9695` |
| SSDT3 | 158797 | `6bd1b3801bc25048e2df874ed81594b3a7014df3f7f1eefc56efb935649e9131` |
| SSDT4 | 9776 | `59b0c0f7f54692fce9c2dcaff6207453a4e5d7adfacaa6a08b942bd0b8d5f835` |
| SSDT5 | 952 | `bdc3e2503248f510997a52bb64c15fdaee013be06d1379acdb279d872cf255ab` |

The DSDT override changes the SSPM `_STA` predicate and OEM revision/checksum.
The ASUS SSDT2 override replaces six EC/battery methods (`RRA9`, `RRA8`, `WRA9`,
`WRA8`, `RWEC`, `GBST`), with scoped return buffers. It does not introduce a
DLA device or DLA power methods. The originals and overrides were both
inspected as static files; no AML method was evaluated for this audit.

## DLA path reviewed

The complete `\_SB_.DLA0` block in candidate DSDT contains only:

- `_HID = NVDA200A`, `_CID = NVDA2042`, `_HRV = 0x34`, `_UID = 0`, `_CCA = 1`.
- `_CRS`: physical `0x19000000/0x40000`, `0x31380000/0x10000`, level/high
  exclusive GSIs 463 and 800.
- `_STA`: return zero if `DLAE` is nonzero, otherwise return 0xf.

`DLAE` is a 32-bit field at offset 0x80 in `_SB.GNVS`, a SystemMemory operation
region at `0x86f50000`, length 0x86. The `_STA` method only reads that field; it
contains no store, call or power-resource transition.

There is no local `_PS0`, `_PS3`, `_PR0`, `_PR3`, `_PSC`, `_PRW` or `_DEP`.
The root and `_SB` namespace declarations in all six candidate tables do not
add those power objects on the parent path. All five SSDTs contain no `DLA0`
reference or declaration; no Alias declaration occurs in the candidate ASL.
Consequently this candidate namespace supplies no DLA-linked PowerResource
whose `_ON`/`_OFF` would need following. Unrelated PCI power resources do exist.
The other DLA0 reference is a device-property package naming `dla`, with tuple
`{1,3,0}`; its contents are not a power-transition method.

`scripts/audit_acpi_namespace.py` indexes decompiled namespace declarations to
make the review reproducible. It is an aid, not an AML interpreter or automatic
safety proof. Each SSDT was disassembled separately with iasl 20251212.
A joint `iasl -da` attempt encountered vendor duplicate namespace objects
(TPM0/PBRR); its partial output was not used as evidence for the SSDT audit.

## SROOT/ROT0 path reviewed

The same archived and candidate six-table sets were reviewed for
`\_SB_.ROT0` / `NVDA8600`, now identified as the SROOT boot provider. The
complete candidate DSDT block (lines 387–406 in its iasl output) contains only
`_HID=NVDA8600`, `_UID=0`, `_HRV=1` and a static `_CRS` with two overlapping
Memory32Fixed descriptors: `0x18010000/0x1000` and `0x18010000/0x10`.
There is no local method, `_STA`, `_INI`, `_PS0..3`, `_PR0..3`, `_DEP`, `_PSC`,
`_PRW`, `_PSW` or `_DSW`. None of those power/initialization objects appears at
root or `_SB_` on its parent path in any of the six tables. The five SSDTs
contain no ROT0 reference; no Alias declaration occurs in either reviewed set.
No ROT0-linked PowerResource is therefore exposed by this candidate namespace.
The live namespace indexes reproduce these declarations. This does not
establish firmware/secure-world side effects of touching the mailbox.

The namespace indexer now accepts `--target`, handles every ancestor prefix
and includes initialization, DMA/coherency and additional power objects in the
parent review. Reproduce the static indexes with:

```sh
venv/bin/python scripts/audit_acpi_namespace.py sources/acpi-candidate --target '\_SB_.ROT0'
venv/bin/python scripts/audit_acpi_namespace.py sources/acpi-archive --target '\_SB_.ROT0'
venv/bin/python scripts/audit_acpi_namespace.py sources/acpi-candidate
```

Both ROT0 indexes contain only the five declarations listed above. The default
DLA0 index is unchanged apart from the generic output key `literal_target_lines`.
As before, the indexer does not interpret AML, resolve arbitrary aliases or
certify binding safety.

## ROT0 DMA/coherency gap

Unlike DLA0 (`_CCA=1`), ROT0 has **no `_CCA`**, and none is declared/inherited on
its candidate root/_SB_ parent path. The installed kernel build configuration
sets `CONFIG_ACPI_CCA_REQUIRED=y`. The local 7.2.8 source was inspected in both
`kernel-recipe/src/linux-7.2.8` and `linux-7.2.8-p16-asus`; the relevant files
match byte-for-byte in both checkouts:

| File | SHA-256 |
| --- | --- |
| drivers/acpi/scan.c | 6d347c65a1c995907f97a3cee50597f2155dd494ae648c0936c3dfb4bac43c26 |
| drivers/base/platform.c | 1c5710433d3b4780d538f88fca11338d4c0967a4b59aad6ade3b175460a91aa0 |
| installed build .config | d114b788fbe20195ec6f37b473a7533c04f315033ec3b56b144d0681040fc939 |

`acpi_init_coherency()` inherits a seen `_CCA` from its ACPI parent or evaluates
the local object. `acpi_dma_supported()` returns false without a seen `_CCA`
when the required option is enabled; `acpi_get_dma_attr()` then returns
`DEV_DMA_NOT_SUPPORTED`. `platform_dma_configure()` passes that attribute to
`acpi_dma_configure_id()`, whose first branch installs **dma_dummy_ops** and
returns zero before architecture DMA or IOMMU configuration. Setting a platform
driver's `driver_managed_dma` flag does not skip this preceding ACPI call.
The upstream [ACPI source](https://code.googlesource.com/linux/torvalds/linux/+/de758035702576ac0e5ac0f93e3cce77144c3bd3/drivers/acpi/scan.c)
also documents the `_CCA` requirement/inheritance rule; the exact running-build
assessment here uses the pinned local source above.

**Live-table finding:** binding an ordinary ROT0 platform driver would not
provide usable DMA merely by setting its 32-bit mask. DLA0 currently has an
IOMMU link; ROT0 does not. The complete live IORT nevertheless explicitly maps
both devices, through different SMMUs:

| Named component / node offset | Address bits | IORT cache / memory flags | Single-map output Stream ID | SMMUv3 node / base |
|---|---:|---|---:|---|
| `\_SB_.DLA0` / `0x930` | 36 | 1 / 3 | 43 (`0x2b`) | `0xa0` / `0x35000000` |
| `\_SB_.ROT0` / `0xc50` | 32 | 0 / 0 | 1 | `0x1a8` / `0x14900000` |
| `\_SB_.SCP0` / `0x12e0` | 32 | 1 / 3 | 96 (`0x60`) | `0x1a8` / `0x14900000` |

All three mappings have SINGLE_MAPPING set and refer to table-relative node
boundaries. ROT0's IORT memory properties describe noncoherent memory; they
are evidence to investigate a noncoherent DMA setup, **not** a substitute for
its missing ACPI `_CCA`. The firmware-declared requester is now identified,
but physical DMA operation and cache maintenance remain untested. The Windows
logical allocation window `0xe0000000..0xffffffff` still needs implementation
through the ROT0 requester rather than through DLA0's domain.

Do not add `_CCA=1`, force direct DMA or reuse DLA0's IOMMU domain on the basis
of shared chip identity. The firmware-declared SROOT requester is identified;
physical DMA operation remains untested. Windows explicitly requests uncached
common-buffer memory, supporting a noncoherent Linux configuration. A scoped
kernel patch is installed in the isolated npu1 test kernel; see
[Linux backend](linux-backend.md) for the still-pending manual boot and DMA
qualification. It is not active in the currently running original kernel. The Windows allocator's returned logical/physical address
separation remains relevant; see [SROOT transport](sroot-transport.md).

## Address discrepancy requiring resolution

The Windows profile chain is now executable offline:

| Windows input ID | Device profile | DLA input / selector | Resource list |
|---|---:|---|---|
| 0x254 / 0x256 | 3 / 4 | 0x303033 / 1 | id0 0x19000000/0x40000; id2 0x31380000/0x10000 |
| 0x354 | 6 | 0x404044 / 2 | id0 0x19340000/0x40000; id1 0x3a340000/0x40000; id2 0x31380000/0x10000 |

Original classifiers, HAL assignments, getters and constructor branch were run
in RAM by `inspect_windows_profiles.py`. Profile 6 selects the firmware parser
for the extracted t35x image. A subsequent original hardware-ID classification
at RVA `0xd3b5c8` now maps the exact string
`ACPI\VEN_NVDA&DEV_200A&REV_0034` to **0x254** (device profile 3 / DLA HAL 1).
`emulate_windows_device_id.py` reproduces this with stubbed IoGetDeviceProperty
and wcsstr imports. Thus the candidate P16 resource list is consistent with the
recovered Windows hardware-ID path. The exact live Windows invocation has not
been captured, and this does not establish remapper mode or auxiliary boot choice.
HAL 1 parses a separate raw firmware at RVA `0x13ac1c0`, size 66816, inner
version 2. T35x-only firmware emulation cannot establish its protocol behavior.

A Linux driver must not combine the new firmware with the old physical aperture
merely because each was found independently. The static Windows resources are
not sufficient grounds to request/map unadvertised physical addresses.

## Remaining live verification

1. Live table collection, identity comparison and DLA/ROT0 ancestor-power
   namespace review are complete. No changed AML or dynamic SSDT was found.
2. The proposed noncoherent ROT0 quirk is scoped and build-tested. Integrate
   it into a recoverable kernel build and verify normal DMA configuration.
3. Establish live remapper/boot state and implement bounded Linux DMA/IRQ/MMIO
   backends before firmware boot, ping/version, tasks and numerical inference.

The observation backend has bound DLA0 and ROT0 without register access. DLA0
confirms Stream ID 0x2b and its SMMU link; ROT0 confirms missing CCA and no DMA
domain. Its resource reservation is fixed; see [Linux backend](linux-backend.md). Offline firmware execution is not a
physical NPU boot or inference result.

## Reproducible live collection and standard IORT decoding

`scripts/collect_acpi.py` now provides the needed read-only collection as a
reusable command. It requires DSDT and IORT, includes static and dynamic SSDTs,
keeps same-named static/dynamic tables separate, checks signature/length/checksum,
and writes per-table SHA-256 metadata into a **new** directory. It rejects an
inventory change observed during reading; this is not an atomic firmware
snapshot guarantee. No AML is evaluated, device bound or register accessed.
When invoked with sudo, it reads the table bytes first and drops to the invoking
user before creating analysis files. Existing snapshot directories are refused.

For a fresh snapshot, use a new destination directory:

```sh
sudo python3 scripts/collect_acpi.py sources/acpi-live
```

Authentication completed and the full collection succeeded. The earlier six-table
copy is superseded by `sources/acpi-live` including IORT.

First use the installed ACPICA disassembler:

```sh
for table in sources/acpi-live/*.aml; do
    iasl -d "$table"
done
```

IORT is a data table, despite the collection's `.aml` filename extension; iasl
recognizes its signature and does not execute AML. Inspect its named-component
paths, ID mappings/output references, SMMU nodes and memory properties against
DLA0/ROT0, then compare the AML namespace and parent power paths with this audit.
Do not replace ROT0's missing `_CCA`, select a DMA requester or declare coherency
merely from a similarly named IORT node or one coherency field.

**An iasl exit code of zero is insufficient evidence of a complete decode.**
The installed 20251212 build can output an unsupported-revision warning and raw
table only, and a zero-length-padding case can stop subtable decoding with an
`Invalid zero length subtable` diagnostic. Review both the log and generated
`.dsl`; partial output is not a completed IORT audit. If the real table triggers
one of these cases, inspect its raw bounds using the actual kernel/ACPICA table
structures rather than modifying the live table to suit the tool.

Local format references are the running kernel's Linux 7.2.8
`include/acpi/actbl2.h` (common node, named component, SMMUv3 and ID-map layouts)
and `drivers/acpi/arm64/iort.c`. The latter treats `id_count` as number of IDs
**minus one**, and handles single-map nodes separately. An eventual route audit
must preserve those distinctions and must not equate `_CCA` policy with IORT
cache fields.

`test_acpi_collection.py` checks static/dynamic collisions, unchanged existing
snapshots, malformed headers/lengths/checksums and missing IORT. It additionally
passes a wholly synthetic revision-6 named-component/SMMUv3 fixture through the
installed iasl and verifies decoded fields without warnings. This tests the
collection/tool workflow; none of its paths, mappings or addresses are evidence
about the P16. Live identity and firmware-declared routing are now established;
physical DMA behavior remains unverified.

## Complete live IORT decode

On this actual table, iasl 20251212 stopped after the first named component
with `Invalid zero length subtable`, despite returning zero. The read-only
`scripts/inspect_iort.py` therefore walks node lengths using the pinned kernel
layouts, rather than walking padding as nodes. It checks signature, total
length, checksum, each node and mapping bound, NUL-terminated component names,
and every output reference against actual node boundaries. It consumes all
6160 bytes and all **93 nodes**. The initial five SMMUv3 bases agree with the
usable iasl prefix. Reproduce without privileges:

```sh
python3 scripts/inspect_iort.py sources/acpi-live/IORT.aml
python3 scripts/audit_acpi_namespace.py sources/acpi-live
python3 scripts/audit_acpi_namespace.py sources/acpi-live --target '\_SB_.ROT0'
```

This decoder reports firmware declarations; it does not resolve Linux DMA
policy, exercise a requester, or establish physical coherency.
