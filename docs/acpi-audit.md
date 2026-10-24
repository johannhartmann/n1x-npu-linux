# P16 ACPI power-path audit

Audit date: 2026-10-25. This is an **archived/candidate-table audit**, not a
verified dump of the currently running firmware. Do not treat it as permission
to bind an NPU driver before the live-table comparison.

## Sources and identity

The original six tables are readable in
`/home/johann/Projects/p16-hardware-fixes/firmware-original/` (2026-10-20).
The candidate set replaces DSDT and SSDT2 with the existing files from
`acpi-extracted/kernel/firmware/acpi/`. Those two hashes match the pinned output
hashes in `p16rtx_linux/acpi/acpi_builder.py`; all six ACPI checksums pass.
Candidate lengths match the lengths observed in live sysfs, but lengths alone
cannot establish identity. Raw tables remain in ignored `sources/`.

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
This result does not establish the live namespace or firmware/secure-world
side effects of touching the mailbox.

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

**Inference conditional on live tables matching:** binding an ordinary ROT0
platform driver would not automatically provide usable DMA merely by setting
its 32-bit mask. Read-only sysfs currently shows DLA0 in IOMMU group 19 with a
SMMU link, whereas ROT0 has neither link. Absence of a link on an unbound device
does not prove absence of an IORT mapping, DMA requester or physical capability.
The pending live-table audit must also include **IORT**, before choosing a
SROOT DMA device/domain and satisfying the Windows logical window
`0xe0000000..0xffffffff`.

Do not add `_CCA=1`, force direct DMA or reuse DLA0's IOMMU domain on the basis
of shared chip identity. Cache coherency and the actual SROOT DMA requester
still need evidence. The Windows allocator's returned logical/physical address
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

1. Obtain a read-only dump of current DSDT, **IORT** and **all** SSDTs, including dynamically
   loaded tables if present, and compare hashes to this candidate manifest.
2. Re-audit any differences on the DLA and ROT0/parent power paths before binding. ACPI can
   perform power transitions before a driver's probe callback executes.
3. Establish the live hardware profile/aperture and remapper mode independently.
4. Only then attach MMIO, power/reset and bounded DMA backends, boot the firmware,
   verify ping/version, and proceed to task submission and numerical inference.

The already-running read-only pkexec copy awaits system authentication. It has
not produced a live dump. No NPU driver was bound and no NPU MMIO was issued.
