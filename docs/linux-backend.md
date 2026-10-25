# Linux platform backend

The current `n1x_npu_observe.ko` is an observation module, not a working NPU
inference driver. It builds against the running P16 kernel. It is not installed
or auto-loaded and has no register-write implementation. Loading is explicit.

## Scope and operation

`driver/n1x_npu_linux.c` matches NVDA200A and NVDA8600 and refuses a probe unless
DMI vendor/product/BIOS are ASUS / ProArt P16 H7607BA / H7607BA.300, hardware
revision is respectively 0x34 / 1, and both memory descriptors exactly match
the [audited live tables](acpi-audit.md). It claims/maps only the first memory
resource. ROT0's second overlapping descriptor supplies spacing, not another
region to claim. No auxiliary aperture is mapped and neither IRQ is requested.

Probe does not read MMIO, request firmware, allocate DMA memory or invoke a
power/reset operation. Standard Linux binding still performs ACPI/core DMA
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
