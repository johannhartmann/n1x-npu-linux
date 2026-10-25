# Linux platform backend

The current `n1x_npu_observe.ko` is an observation module, not a working NPU
inference driver. It builds against the running P16 kernel. It is not installed
or auto-loaded and has no register-write implementation. Loading is explicit.

## Scope and operation

`driver/n1x_npu_linux.c` matches NVDA200A and NVDA8600 and refuses a probe unless
DMI vendor/product/BIOS are ASUS / ProArt P16 H7607BA / H7607BA.300, hardware
revision is respectively 0x34 / 1, and both memory descriptors exactly match
the [audited live tables](acpi-audit.md). DLA maps its first memory resource.
ROT0's second overlapping descriptor supplies spacing and also appears as a
child in Linux's resource tree. Reserving its entire parent resource fails
with EBUSY even without another driver: the requested parent extent crosses
that child. The backend therefore claims/maps only the actual register span
for indices 1..12: **0x18010010..0x180100c3**. Logical mailbox offsets remain
relative to 0x18010000 and are translated/bounds-checked against this subrange.
It neither removes the child descriptor nor bypasses resource reservations.
No auxiliary aperture is mapped and neither IRQ is requested.

With default parameters, probe does not read MMIO, request firmware, allocate
DMA memory or invoke a power/reset operation. The explicit `test_dma=1` option
adds the allocation-only check described below. Standard Linux binding still performs ACPI/core DMA
configuration, including IOMMU/domain setup where available. The preceding
power-path audit found no DLA/ROT0 or ancestor initialization/power methods
that binding would execute in these exact tables. This is not a guarantee that
later register accesses are side-effect-free on untested hardware.

`dma_state` reports the actual ACPI cca_seen/coherent flags, Linux DMA-coherency
flag, IOMMU domain type (or -1), and firmware-specification Stream IDs. These
are configuration observations, not evidence that a device has transferred data.
The IOMMU specification is inspected for diagnostics only; it is not modified.

The immutable load-time parameter `read_mmio=1` enables an explicit root-only
`registers` read. DLA logical offsets are 0x4c, 0x8, 0x40, 0x44 and
0xd2c, whose physical translations agree in both recovered remapper tables.
That agreement does not identify the active remapper mode. ROT0 reads status
indices 2, 5, 8 and 11 at 16-byte spacing. It never reads the consuming data
FIFO indices 4 or 7. No status register is read merely by loading the module.
A snapshot comprises sequential reads, not an atomic hardware observation.

```sh
make module
sudo insmod driver/n1x_npu_observe.ko
cat /sys/bus/platform/devices/NVDA200A:00/dma_state
cat /sys/bus/platform/devices/NVDA8600:00/dma_state
sudo rmmod n1x_npu_observe
```

For a separately selected register-observation run, load with `read_mmio=1`
and read the respective `registers` attributes as root. Do not combine this
module with another driver owning either aperture. There are no autoload
aliases, services, persistent module options or boot-entry changes.

## SROOT DMA prerequisite

The live IORT routes ROT0 Stream ID 1 through SMMUv3 at 0x14900000, separately
from DLA0. Its memory properties declare noncoherent access. The original
Windows provider explicitly requests a **MmNonCached** common buffer, confirmed
with a poisoned-stack emulator run. ROT0 lacks `_CCA`, which makes the normal
ARM64 ACPI path reject DMA despite the IORT mapping. Merely setting a 32-bit
DMA mask is insufficient.

`patches/linux-7.2.8-p16-sroot-noncoherent-dma.patch` is a proposed kernel-side
fix for this exact audited BIOS/device revision. It treats only a missing
`_CCA` on that ROT0 as noncoherent, before standard ACPI/platform DMA setup.
Existing local/inherited `_CCA`, other devices, other BIOS revisions and
architectures not requiring `_CCA` retain their existing behavior. The patch
does not force direct DMA, mark the device coherent, choose a foreign domain,
or alter an IORT mapping. It enables the normal kernel route to configure the
firmware-declared requester and perform architecture cache handling.

The patch is **not applied to the running kernel or installed boot artifacts**.
It applies without fuzz to the inspected Linux 7.2.8 source. The patched
`drivers/acpi/scan.c` compiles against the running kernel's headers/configuration.
The actual patched coherency function also passes 13 mocked policy/precedence
cases, including inherited/local `_CCA`, failed evaluations, revision and DMI
mismatches and architectures not requiring `_CCA`:

```sh
python3 scripts/test_sroot_cca_quirk.py /path/to/linux-7.2.8
```

These checks establish patch scope/build compatibility; successful boot and DMA
remain untested. Integrate into a separately recoverable kernel build before
using the normal SROOT DMA API. Do not patch ACPI flags after binding or assume
that rerunning `acpi_dma_configure_id()` removes an earlier dummy DMA backend:
ARM64's `arch_setup_dma_ops()` itself does not clear it.

The physical upload backend must still allocate through ROT0, verify that the
**entire** logical allocation lies in 0xe0000000..0xffffffff, publish copied
firmware with DMA ordering, and retain the buffer across uncertain transport
failures until DMA is proven quiescent. The ordinary coherent allocation API
on a correctly configured noncoherent device can provide suitable shared CPU
mapping; its name does not assert hardware coherence. A successful allocation
outside that logical window must be rejected before any request is submitted.

Firmware boot, actual DMA/IRQ delivery, task execution and numerical inference
are not implemented or verified by this observation module.

## Physical observation so far

On 2026-10-25 the observation module loaded successfully on kernel
7.2.8-23-omarchy-p16-asus. DLA0 bound with register reads disabled:

```text
acpi_cca_seen=1 acpi_coherent=1 dma_coherent=1 iommu_domain=4
stream_id[0]=0x2b
```

The sysfs IOMMU link resolves to SMMUv3 0x35000000, matching the live IORT.
No DMA allocation or transfer was attempted. ROT0's initial whole-resource
reservation failed with EBUSY. A revised observation module uses the restricted
register span above and reloaded successfully at 08:01:06 (kernel journal).
Both devices are now bound with MMIO reads disabled. ROT0 reports:

```text
acpi_cca_seen=0 acpi_coherent=0 dma_coherent=0 iommu_domain=-1
```

A privileged `/proc/iomem` read confirms the two firmware resources and the
backend's distinct reservation:

```text
18010000-18010fff : NVDA8600:00
  18010000-1801000f : NVDA8600:00
  18010010-180100c3 : NVDA8600:00 NVDA8600:00
```

This verifies the resource fix on hardware. SROOT DMA remains unconfigured;
the missing-CCA kernel patch is not installed. No firmware boot, DMA transfer,
IRQ handling or inference has been attempted. The observation module remains
loaded for inspection, with no persistent autoload configuration.

## Identity-domain prerequisite

The live value `iommu_domain=4` is **IOMMU_DOMAIN_IDENTITY**, not a translated
DMA domain. `/sys/kernel/iommu_groups/19/type` and group 34 (SCP0) both report
`identity`. The running configuration has `CONFIG_IOMMU_DEFAULT_PASSTHROUGH=y`;
there is no command-line override. A DMA mask limits addressability; it does
not by itself allocate IOVAs in the Windows 0xe0000000..0xffffffff window.

The missing-CCA patch enables SROOT's normal ACPI DMA setup but does not change
this default policy. A physical upload backend must additionally establish and
check the intended domain before allocating. Do not infer translated DMA from
an IOMMU link or a non-NULL domain. Require `iommu_is_dma_domain(domain)` for a
backend relying on the kernel IOVA allocator and reject identity/unmanaged/
missing domains before publishing a firmware DMA address.

Linux 7.2.8 supports a per-group `type` sysfs control accepting `DMA`. It
requires all drivers in the group to be unbound for an identity -> DMA switch.
The actual group membership and device-to-SMMU links must first be checked;
group numbers are not stable. Only groups containing the explicitly audited
NPU/SROOT devices may be considered. A future test-kernel setup can select
those groups individually before loading the NPU backend, rather than changing
the global IOMMU policy for the GPU and unrelated devices. No group type has
been changed during this analysis. Actual domain switching, allocation and
hardware DMA remain pending.

## Isolated test-kernel build

`scripts/build_sroot_kernel.py` builds a new experimental release named
`7.2.8-23-omarchy-p16-asus-npu1`. It requires an already-built source tree whose
configuration and ARM64 Image match the installed running 7.2.8-23 package
byte-for-byte. It copies that tree into a new directory, applies the SROOT
patch without fuzz, adds a localversion suffix, and builds Image/modules.
A copied-source observation module is then built against the new kernel, so
its different vermagic does not overwrite the running-kernel module artifact.
The manifest records base/patch/output hashes and the build stage.

```sh
python3 scripts/build_sroot_kernel.py \
  /home/johann/p16rtx_linux/kernel/src/linux-7.2.8 \
  /home/johann/n1x-lab/npu/results/linux-7.2.8-p16-npu1 --jobs 8
```

The destination must be new. Do not rerun an interrupted build blindly; inspect
its actual process/log first. The script neither installs modules/packages nor
changes initramfs/UKIs, boot entries or the boot default. A completed build is
not proof of successful boot. Deployment still needs a separate entry retaining
the known-working 7.2.8-23 kernel, the same audited ACPI overrides, and compatible
NVIDIA/other external modules for the new release. Firmware boot/inference
remain separate tests after the kernel and DMA setup are established.

The first isolated build completed successfully. Its Image SHA-256 is
`b7814a84b13d146b712555de9e4c6ad341d53895d7147282e20e186a3f22ee32`.
The base configuration SHA-256 remains
`d114b788fbe20195ec6f37b473a7533c04f315033ec3b56b144d0681040fc939`;
the original source tree's Image/config remain unchanged. The observation
module also compiled against the new release with matching vermagic. The
running-kernel module artifact still targets the original 7.2.8-23 release.

To prepare the same installed external-driver versions in copied sources:

```sh
python3 scripts/build_sroot_external_modules.py \
  results/linux-7.2.8-p16-npu1 results/npu1-external-modules --jobs 8
```

This completed for NVIDIA 615.78.08 (`nvidia`, `nvidia-drm`, `nvidia-modeset`,
`nvidia-uvm`, `nvidia-peermem`), spark_hwmon `spbm` 0.3.0.rtxspark.5 and cpuid
0.8.2. All seven output modules have the expected test-kernel vermagic. The
script preserves `/usr/src` and the live DKMS database, refuses an existing
output directory, verifies the completed kernel Image, and records module
hashes. No external modules were installed or loaded. These are build checks;
GPU/display/hwmon operation under the new kernel is untested.

`test_sroot_kernel_build.py` verifies six failure guards: invalid job count,
nested/same source destination, mismatched running configuration/Image, and
an existing output directory. Its fixtures perform no copy/build/install.
Both builders intentionally refuse implicit resumption; inspect a failed or
interrupted process and its exact output before choosing how to resume.

## Allocation-only DMA qualification

The observation module now has an immutable load-time `test_dma=1` option.
It is disabled by default. The updated module compiles for both the current kernel and the isolated test
kernel; this is not a physical allocation result.

For each device the test requires an observed ACPI `_CCA`, matching ACPI/Linux
coherency, a non-NULL DMA mask, a DMA-API domain and the kernel's active
`use_dma_iommu()` backend. Identity, unmanaged and missing domains are rejected
before allocation. It then sets the mask to 32 bits for SROOT or 36 for DLA.
SROOT allocates **67508 bytes**, matching the audited default opaque upload
image; DLA allocates 4096 bytes. The complete SROOT extent must lie within
**0xe0000000..0xffffffff**. Both addresses must be 256-byte aligned, and zero
is rejected for DLA's encoded-method use. The test checks CPU write/readback
and frees the buffer.

No device is given the address: no firmware is requested or copied, no mailbox
request is sent, and no NPU register is written. Therefore the buffer can be
freed after every allocation outcome. **That cleanup rule must not be reused
after a firmware upload/task attempt**, where delivery/DMA quiescence may be
uncertain. The test establishes allocation/mapping constraints only; it cannot
establish device DMA visibility, transfer completion or inference.

`test_linux_dma_allocation.py` compiles the actual kernel C allocation function
with mocked API boundaries. Twenty-three cases verify prerequisites prevent
allocation, whole-extent/address/alignment limits, mask/allocation/readback
failures, and exact allocation/free pairing. These are fault checks, not real
DMA tests.

`scripts/select_npu_dma_domains.py` prepares only the two audited device groups
on the exact `...-npu1` test kernel. Its default mode prints a plan. Before any
write, it checks machine/BIOS, both devices unbound, exact SMMU suppliers,
single-device groups and supported current types. With `--apply` it changes
identity to DMA and preserves existing DMA/DMA-FQ domains. It never unbinds a
driver, changes global policy, or forces a rejected kernel operation. Kernel
locking/ownership checks also guard the actual type writes. Nine filesystem
fixtures check shared groups, bound devices, wrong suppliers/BIOS/types/paths,
and preservation of already configured domains. No real group was changed.

After the test kernel is separately installed and successfully booted, the
intended qualification sequence is:

```sh
# Unload the observation module first if it is loaded; both devices must be unbound.
python3 scripts/select_npu_dma_domains.py
sudo python3 scripts/select_npu_dma_domains.py --apply
sudo insmod results/linux-7.2.8-p16-npu1/n1x-npu-module/driver/n1x_npu_observe.ko test_dma=1
cat /sys/bus/platform/devices/NVDA200A:00/dma_state
cat /sys/bus/platform/devices/NVDA8600:00/dma_state
journalctl -k -b --no-pager -g 'DMA allocation-only test|DMA allocation test failed'
```

A loaded module alone does not imply either probe/test succeeded; verify both
device bindings and their journal results. A failed second domain change can
leave the first group configured; inspect the actual type controls before
continuing. This sequence has **not** run on hardware. Test-kernel packaging,
boot-entry preparation and boot verification still precede it. The existing
kernel and global IOMMU configuration remain unchanged.

The negative prerequisite test has now also run on the unchanged physical
7.2.8-23 kernel (journal 2026-10-25 08:37:00). Both probes rejected `test_dma=1`
with **-EOPNOTSUPP** before allocation: DLA has an identity domain; SROOT lacks
CCA/DMA configuration. The temporary module was removed and reloaded with both
`test_dma` and `read_mmio` disabled. Both ordinary observation bindings were
restored, with their original DMA states. The loaded module's srcversion
matches the updated current-kernel artifact. No successful physical allocation
or NPU transfer is claimed from that negative test.

## Prepared kernel packages

`scripts/package_sroot_kernel.py` reuses the pinned P16 packaging functions
from the existing 7.2.8-23 recipe with a distinct package name and the already
verified test-kernel build. It performs no source downloads or rebuilds and
does not install anything. Kernel/header files use only the new release's
module, source and DTB paths; the packages declare no conflicts/replacements.
The package audit checks those paths, identity/architecture, exact Image/config
hashes, header release and retained SPI HID modules.

```sh
python3 scripts/package_sroot_kernel.py \
  results/linux-7.2.8-p16-npu1 \
  /home/johann/p16rtx_linux/kernel/PKGBUILD results/npu1-kernel-packages
```

This produced and audited:

- `linux-p16-asus-23-npu1-7.2.8-23-aarch64.pkg.tar.zst` (165788954 bytes).
- `linux-p16-asus-23-npu1-headers-7.2.8-23-aarch64.pkg.tar.zst` (41561820 bytes).

The observation module also compiles against a separately extracted copy of
the **packaged headers**, with matching test-kernel vermagic. Neither package
was installed. Initramfs/UKI preparation, controlled boot-entry creation and
boot verification are still pending. Normal package installation may run
system-wide DKMS/boot hooks; deployment must account for those before invoking
pacman and must preserve the existing boot default/recovery entry.
