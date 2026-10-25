# Linux platform backend

The current `n1x_npu_observe.ko` is an observation module, not a working NPU
inference driver. It builds against the running P16 kernel. It is not installed
or auto-loaded and has no register-write implementation. Loading is explicit.

## Live test-kernel and allocation result

On 2026-10-25 the machine booted **7.2.8-23-omarchy-p16-asus-npu1**,
boot ID `7620df9d-81b4-4d49-87e7-a38fac273619`. The preboot audit checked the
installed UKI, fallback EFI digests, unchanged menu defaults, eight installed
modules and boot-critical UKI contents. NVIDIA 615.78.08 answers `nvidia-smi`;
the input subsystem enumerates the ASUS keyboard and touchpad. This does not
replace the user's functional check of the display and input devices.

At 17:49:27 local time, the prepared allocation-only procedure succeeded on
both devices. Before changing anything, the domain selector verified each
device was unbound, in a dedicated group, and attached to the expected SMMU.
It changed only groups 19 (DLA) and 24 (SROOT) from identity to DMA. Group
numbers are observations of this boot, not configuration constants.

| Device | ACPI CCA seen / coherent | Linux coherent | Domain / Stream ID | Allocated extent |
| --- | --- | --- | --- | --- |
| `NVDA200A:00` | `1 / 1` | `1` | DMA (`3`) / `0x2b` | 4,096 bytes at `0x0ffffff000` |
| `NVDA8600:00` | `1 / 0` | `0` | DMA (`3`) / `0x1` | 67,508 bytes at `0xfffe0000` |

Both probes logged successful allocation/CPU readback and freed their buffers.
The entire SROOT extent fits the required `0xe0000000..0xffffffff` window;
both addresses meet the 256-byte alignment requirement. This verifies the
missing-CCA quirk and allocator prerequisites on the real test kernel without
forcing SROOT coherent. The loaded observation module has `test_dma=1` and
`read_mmio=0`; both devices bind to `n1x-npu-observe`.

The module neither published those addresses to hardware nor requested
firmware. There was no NPU register read/write, mailbox transaction or device
DMA transfer. Firmware bootstrap, DMA visibility to the device, completion,
IRQ routing and numerical inference remain unverified. The root-terminal
execution record and kernel messages are saved locally in ignored
`results/npu1-allocation-live.jsonl` and `results/npu1-first-live-allocation.json`.

The build/install sections below retain intermediate historical observations;
their earlier pending-boot/allocation statements are superseded by this record.

### First upload preflight and runtime-PM correction

After explicit approval for a single firmware upload, the pinned opaque image
was installed at `/usr/lib/firmware/nvidia/n1x/p16-sroot-v0.bin`, and the
bootstrap module replaced the observation module. Both endpoints bound.
The first sysfs upload call failed with `-EACCES` (`-13`) while
`attempted=0`, `dma_retained=0`, `reply_valid=0` and `idle_observed=0`.
The kernel emitted no upload-phase message. This was a host preflight failure,
not a firmware rejection or a completed mailbox transaction.

Both devices reported `power/runtime_status=unsupported`. The driver had
called `pm_runtime_resume_and_get()` without enabling runtime PM in probe.
The inspected Linux 7.2.8 `drivers/base/power/runtime.c` returns `-EACCES`
for that disabled state before dispatching resume. See the upstream
[runtime-PM lifecycle documentation](https://docs.kernel.org/power/runtime_pm.html).

Probe now calls `devm_pm_runtime_enable()` after mapping the audited resource
and before publishing the endpoint. It preserves normal subsystem resume
callbacks; it neither forces an ACTIVE state nor skips PM callbacks. Managed
cleanup disables runtime PM on failed probe/removal. Runtime suspend returns
`-EBUSY`, since the experiment has no established device-quiescence/power-down
protocol. Actual per-device resume failures are logged before releasing the
firmware and any earlier runtime reference.

The corrected module builds for the test kernel; the upload lifecycle suite
includes both devices returning `-EACCES` and verifies no allocation,
publication or retained reference on those failures. The prepared reload
requires the exact old `attempted=0`, `error=-13` state and no outstanding
operation/storage before unloading. It must never be used after a published
upload. The corrected module was then loaded in the same boot. Both devices
changed from `unsupported` to `suspended` before the trigger and to `active`
after it; the upload passed this previously failing preflight.

### First physical SROOT upload reply

At **17:58:30** the corrected module performed the first actual mailbox upload.
The kernel logged `upload phase returned -121`, `reply_valid=1`,
`raw=0x2a006018`, `idle=0`, with SROOT DMA `0xfffe0000` retained until reboot.
`-121` is `-EREMOTEIO`: the transport returned a reply, but its decoded
status/detail was nonzero. The helper therefore did not run the DLA idle wait.
This is not a successful firmware boot and does not prove that SROOT read the
staged image or that any engine is ready.

Replaying the **actual reply header** through original Windows receive/decoder
code (`0x5328` / `0x4f70`) and the Linux receive helper produces the same tuple:

| Field | Observed value |
| --- | --- |
| Command | `0x18`, matching the upload request |
| Status | `6` |
| Additional/detail byte | `0x2a` (42) |
| Payload words | `0` |

Original Windows upload cleanup also returns `0xc0000229` for the resulting
nonzero status word `0x2a006`. The meanings of status 6 and detail 42 remain
undocumented; no authentication, DMA or hardware-variant cause is asserted.
`test_sroot_receive.py` includes this reply as an original-code regression.
The live reports are `results/npu1-first-upload.jsonl` (preflight failure) and
`results/npu1-pm-fixed-upload.jsonl` (the actual transport attempt).

The Python trigger initially used `Path.write_text()`. On a sysfs write error,
buffered close/flush issued a second write; a RAM-only `RawIOBase` reproduction
observed two writes and the same overwritten error. The kernel's one-attempt
guard rejected the second call before hardware access, but the old sysfs
handler replaced its recorded `-EREMOTEIO` with `-EALREADY` (`-114`). The kernel
journal preserves the first result, and the reply header remains unchanged.
There was one mailbox upload, not a second hardware attempt.

Future triggers use `scripts/npu_trigger.py`, which opens the attribute and
calls unbuffered `os.write()` without a short-write loop or an explicit retry;
closing the descriptor cannot flush buffered data. Kernel operation guards
remain authoritative, including for interrupted calls. The updated upload
handler also preserves the original published operation's error when a later
call is rejected. Seven userspace trigger cases and the compiled sysfs handler
tests cover failure, short-write handling and first-result preservation.

The first-result-preservation change is built and tested but **not loaded**:
the live module and firmware buffer remain pinned after the real attempt.
No ping, config import, bypass task, retry, unload, reset or reboot followed it.

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

The patch is now **active in the running `npu1` test kernel**. The live
allocation result above confirms the intended noncoherent SROOT configuration.
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
the missing-CCA kernel patch was not installed at that observation time. It is
now installed only in the unbooted test kernel. No firmware boot, DMA transfer,
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

## Staged UKI and release-specific ACPI hook

The existing installed `p16-acpi` hook and its configuration allow only the
known kernel releases. The NPU test release would otherwise omit the ACPI
fixes or fail that hook's guard. `build_sroot_uki.py` therefore builds a
**private module root and hook directory**, retaining the exact pinned tables
through a local `npu-test-acpi` hook. It snapshots the current mkinitcpio base/
drop-in configuration, preserves the current command line, adds SPI HID, and
copies the seven hash-checked matching external modules into the staging root.
`depmod` and module-vermagic checks use that root, not the running system.

Only `run_sroot_mkinitcpio.py` requires root. It reads the current Limine menu
into the private build directory and runs mkinitcpio with **--nopost** and an
explicit output under `results/`. Thus it creates no installed modules,
initramfs presets, EFI files under `/boot`, or boot-menu entries. The output is
returned to the invoking directory owner's UID/GID. No NPU driver is loaded
by this build. Standard hook directories are passed explicitly in addition to
the private one: mkinitcpio's `-D` **replaces**, rather than augments, its defaults.

```sh
python3 scripts/build_sroot_uki.py \
  results/linux-7.2.8-p16-npu1 results/npu1-kernel-packages \
  results/npu1-external-modules results/npu1-uki-v3
```

The initial offline attempt lacked standard hooks because of `-D` behavior;
mkinitcpio nevertheless returned zero. A second attempt restored the hooks but
reported fakeroot internal metadata-protocol errors. Both images are marked
invalid and renamed `.efi.invalid`. Neither was installed. The builder now
uses real root metadata, records the build log and rejects mkinitcpio errors
independently of the exit status; fakeroot is no longer used for UKI creation.

The **third build completed with a clean log** and passed
`verify_sroot_uki.py`. GNU objcopy extracts the PE sections into temporary
files without rewriting the input UKI. Verification checks:

- Exact test-kernel release, Image bytes and command line.
- Early-CPIO DSDT/SSDT contents against the pinned SHA-256 values.
- NVIDIA, SPI/I2C-HID and ASUS keyboard modules.
- Init, encrypt hook, cryptsetup and vconsole configuration presence.
- No observation module in the initramfs, so it cannot bind the NPU at early boot.

`test_sroot_uki_verifier.py` checks a valid early CPIO and seven malformed/
duplicate/truncated fixtures. The real artifact is
`results/npu1-uki-v3/linux-p16-asus-23-npu1.efi`, 212898304 bytes, SHA-256
`f05f2a6d04876655cbdbf8f45da22189eb1da31355cfb16b8596088083f4727e`.
Its manifest records the exact configuration snapshot and initial menu hash.

The inspected menu still selects **Omarchy/linux-p16-asus-23** by default,
with a five-second timeout. No menu or installed EFI image has been changed.
Package/module installation and adding a separate test entry remain pending;
physical boot and successful DMA allocation are still unverified.

## Additional test-entry installation

`install_sroot_test.py` installs the two prepared kernel/header packages and
copies the eight prebuilt external/observation modules into the **new release's**
`updates/npu-test/` directory. Those extra module files are recorded by path/hash
in the root-owned checkpoint; they are not registered in the live DKMS database.
The installed kernel/header files remain package-managed. Nothing is loaded
into the running kernel by this installation.

Before any mutation it rechecks the machine, working kernel, EFI mount/free
space, package/module/UKI hashes, actual UKI contents and unchanged configuration
snapshot. Under the existing boot-partition lock it also checks the unchanged
Limine snapshot and the BLAKE2b hash of the current default EFI image. Existing
test destinations/checkpoints cause a refusal rather than an implicit retry.
The root-owned checkpoint is `/var/lib/n1x-npu-test/npu1/`.

Only this pacman transaction receives a temporary hook-directory mask. Existing
hook files remain untouched. Thus DKMS, initramfs and bootloader package hooks
cannot rewrite working images/defaults during this isolated installation. The
installer uses the verified prebuilt modules, runs depmod for the new release,
and checks their vermagic/dependency paths. It copies the already checked UKI
into a new EFI filename and verifies its on-disk hash before appending an entry.

The new top-level menu entry is **N1X NPU test**. The complete original menu
remains its byte-for-byte prefix, including the old default/timeout and old EFI
paths. The new entry uses the on-disk image's BLAKE2b URI digest and the checked
command line. No reboot or NPU operation is performed. The menu-generation
function passes two append-only fixtures and five invalid-input cases, and a
concrete proposal has been rendered from the actual captured menu.

```sh
sudo python3 scripts/install_sroot_test.py \
  results/npu1-uki-v3 results/linux-7.2.8-p16-npu1 \
  results/npu1-kernel-packages results/npu1-external-modules
```

If interrupted, inspect the actual package/module/EFI/menu state and checkpoint;
do not rerun blindly. A failure before the final append leaves the old menu
unchanged, even if the new packages or unused EFI file already exist. The final
menu write is atomic; its error path restores the previous bytes if the written
proposal is still present. The original boot entry remains the default and is
the recovery route. Successful boot, graphics/input and NPU DMA qualification
still require explicit observation after manually selecting the test entry.

The test installation has now completed successfully. The privileged installer
reported **ready-for-manual-boot**, after validating the old default EFI hash,
the new on-disk EFI hash, installed module dependencies and unchanged menu
prefix/default/timeout. Kernel/header packages are registered, the installed
Image/config match the build pins, and all eight copied module hashes and
vermagics match their verified artifacts. The root-owned checkpoint remains at
`/var/lib/n1x-npu-test/npu1/state.json`.

The original `7.2.8-23-omarchy-p16-asus` kernel is still running. Select
**N1X NPU test** manually on the next boot; an ordinary unattended boot keeps
the old default. Confirm `uname -r` is
`7.2.8-23-omarchy-p16-asus-npu1` before attempting domain/allocation qualification.
No reboot has been initiated by the installer or agent. Boot, graphics/input,
successful DMA allocation, firmware start and inference remain unverified.
Build manifests describe their generation-time state; the root-owned installation
checkpoint and actual package/filesystem state are authoritative for deployment.

Before selecting the test entry after further development, recheck the actual
installation with the read-only audit:

```sh
sudo python3 scripts/audit_sroot_test.py > results/preboot-audit.json
```

It verifies the root-owned completion checkpoint, installed UKI and kernel
hashes, exact append-only Limine menu, working fallback's BLAKE2b digest,
installed module hashes/vermagics/dependencies, and the UKI's kernel, command
line, ACPI overrides and required boot files. It neither repairs discrepancies
nor changes the default, loads modules or reboots. A successful result permits
the planned manual boot experiment; it does not prove that the kernel boots.
The command needs root only because `/boot` and the checkpoint are protected.
An unprivileged `pacman -Qk` can check headers/root files but cannot inspect the
EFI mount's DTB paths because `/boot` is restricted; those permission errors
must not be reported as proof that files are missing.

### Raw HAL-1 low-power delay consumer

`test_profile1_lpwr_consumer.py` executes original function `0x107318` after
original reset initialization. Its input is the command-23 block at `0x186254`.
The unaligned little-endian word at offset 2 is multiplied by 1000 with a full
64-bit product. The timer helper `0x1053ba` reads the low/high timer registers
at MMIO offsets `0x2c`/`0x30`, including its original rollover-consistency logic.
The consumer subtracts the saved 64-bit value at `0x185ba8` modulo 2^64 and
notifies only when elapsed is **strictly greater** than the product.

Byte 1 equal to 2 suppresses notification. Values 0, 1 and 255 all pass this
particular check; that does not establish their validity throughout the ABI.
An already-notified byte at `0x18627d` or nonzero activity word at `0x186250`
also suppresses notification. On the notification path the firmware calls
`0x10044c` with arguments 12, 0 and a pointer to twelve zero bytes, then sets
the already-notified byte to 1. The test intercepts that outbound transport
boundary, not the timer or arithmetic functions. Optional diagnostic DMA is
disabled by an explicit synthetic context flag.

396 cases cover flag values, delay zero/one/19200/UINT32_MAX, threshold
boundaries, suppression gates and saved timestamps around 32/64-bit rollover.
The firmware code bytes remain unchanged. These are synthetic, stable timer
registers: the test does not exercise a timer changing between individual reads.
Neither physical tick duration nor the Windows provider field's units are
established. The L4T `idledelay_us` correspondence remains useful evidence,
not a measured P16 calibration. No live firmware boot or inference is implied.

```sh
venv/bin/python scripts/test_profile1_lpwr_consumer.py
```

### Connected initial SROOT upload phase

`driver/n1x_dla_boot.c` now connects the verified callback components for the
initial method-2 upload: `n1x_sroot_exchange(command=0x18, three words)`, check
both decoded firmware status and detail, then `n1x_dla_wait_idle`. It never
calls the direct-loader CPUCTL start function. The separate SROOT and DLA I/O
objects preserve their distinct apertures/device ownership.

`n1x_dla_sroot_upload` requires a request produced by `n1x_sroot_stage_image`
from validated firmware and an appropriately mapped/visible SROOT DMA buffer.
It does not allocate, synchronize, publish a Linux mapping or free anything.
The caller exclusively owns endpoints and storage, provides ordered callbacks,
and serializes the entire operation. Invalid callback/poll parameters cause no
I/O and leave the result unchanged. Once preflight passes, `upload_attempted`
is set **before** calling the mailbox transport and remains set on every return.
This conservative indicator requires retention of upload storage even when a
preparation error occurred before its address was sent. It is not a completion
or quiescence indicator and must not clear ownership of earlier transactions.

A received reply is preserved with `reply_valid=1`, including firmware failures.
Nonzero status/detail returns `-EREMOTEIO` without reading DLA idle. Transport
errors preserve `reply_valid=0`. A subsequent idle error leaves the received
reply available but `idle_observed=0`. A zero return requires both a zero
firmware status/detail and an observed zero IDLESTATE. It proves neither full
boot/readiness nor that freeing upload memory is safe. In line with the observed
Windows wrapper, the helper preserves rather than imposes reply-command echo
identity; its actual live response still needs qualification.

The API deliberately stops before route capture, IRQ-mask changes, ping,
region/configuration setup, task queues and inference. It has separate bounded
budgets per polling stage, not a reconstructed Windows global timeout. It is
compiled as a kernel component and is **not linked into the observation module**.
The separate bootstrap module below now supplies guarded Linux callbacks; neither
that connection nor this phase has been invoked on the machine. Installed
test-kernel/observation artifacts are unchanged.

`test_sroot_upload_phase.py` checks 24 transport traces against the original
Windows wrapper, injects failure at all 22 I/O positions, verifies timeout/busy
and firmware-rejection stop paths, and rejects 14 invalid callback/poll inputs
before I/O. Its DLA interface has no write callback, so an accidental core-start
write cannot silently pass. All readiness, replies and DMA addresses are
synthetic. The original method-2 orchestration, SROOT exchange and core-control
regressions also pass, as does `make kernel-check`.

### Explicit Linux bootstrap module (built, not yet run)

`driver/n1x_npu_boot_linux.c` provides a separate **n1x_npu_bootstrap** module
for the initial SROOT upload phase. It connects Linux firmware loading, SROOT
coherent DMA allocation, ordered MMIO callbacks and the callback phase above.
The observation module remains independent and has no write path. Neither
module has ACPI autoload aliases. Bootstrap upload is disabled by default and
never happens during module initialization, probe or a status read.

This experiment is restricted to `7.2.8-23-omarchy-p16-asus-npu1`, the exact
ASUS/P16/BIOS identifiers and the audited ACPI revisions/resources. SROOT maps
only register indices 1..12 at stride 16, preserving the stride descriptor's
reservation. Its callbacks allow only the recovered TX/RX/status/ACK registers.
The upload phase's DLA interface permits only a read of logical IDLESTATE
`0x4c`; it has **no write callback**. The separate opt-in ping phase below adds
the restricted route/mask/method register callbacks. Logical offsets are
bounds-checked before readl/writel. No IRQ handler,
CPUCTL/reset write, inference UAPI or automatic recovery is implemented.

The root-only `upload` attribute accepts the exact word `upload` and additionally
requires CAP_SYS_RAWIO and the immutable `enable_upload=1` load option. Both
endpoints must be attached. Before allocation it requires the expected ACPI and
Linux coherency, translated DMA domains using the IOMMU DMA backend, the observed
stream IDs (SROOT 1, DLA 0x2b), and a dedicated IOMMU group for each endpoint.
It never changes groups or global IOMMU policy itself.

Only `nvidia/n1x/p16-sroot-v0.bin` is requested, without userspace firmware
fallback. Before allocation/publication the module checks length 67,508 and
SHA-256 **0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4**.
This pins the exact recovered opaque variant; it does not decode/authenticate
its internal format or prove equivalence to the separately emulated raw image.
The SROOT-owned coherent allocation must be 256-byte aligned, entirely in
`0xe0000000..0xffffffff`, and fit the 32-bit DMA mask. Rejected unpublished
allocations are freed. No CPU physical-address substitution, alternate device,
manual IOVA mapping or guessed cache operation is used. The existing stage
helper copies image[4:] and zeros the final four bytes. `dma_wmb()` precedes
ordered mailbox writes.

**After an attempted upload this experiment requires a reboot before another
upload attempt.** The separately enabled ping phase may run once after a
successful upload/idle result. Before any mailbox operation it pins its own module and holds runtime
PM references for both endpoints. It retains the DMA allocation and those
references on **all** transport/firmware/idle outcomes, including success.
Subsequent triggers return EALREADY. It suppresses manual bind/unbind attributes
and rejects system suspend/freeze/poweroff preparation with EBUSY after the
attempt; runtime references prevent normal autosuspend. This is deliberately
not production recovery. Do not force-unload it. Unexpected platform removal,
kexec, physical reset guarantees and a recoverable DMA-quiescence procedure
remain outside the implemented experiment. No kernel reboot is initiated by it.

Before publication, firmware/mask/runtime-PM/allocation/staging/module-reference
failures balance all acquired resources. The firmware file's storage is released
after copying; it is independent of the retained coherent DMA allocation.
Status exposes whether endpoints are bound, upload is enabled/attempted, the
last error, raw reply validity/header, observed idle and allocation retention.
A successful upload phase still does not mean IRQ setup, ping, full boot or
inference succeeded.

Validation performed:

- Actual upload-function lifecycle: 21 fault/success cases, including real vendor
  SHA-256, corruption/size controls, balanced early cleanup, PM/module references,
  retained storage after timeout/rejection and refusal to repeat an attempt.
- Actual machine/DMA/MMIO guard functions: 544 cases plus machine/kernel and
  resource-end controls. Identity domains, missing/mismatched CCA/coherency,
  unexpected/multiple stream IDs, shared groups and forbidden offsets fail.
- Connected phase's 24 Windows transport comparisons / 22 I/O fault positions /
  14 invalid-input controls still pass. All OS/device effects in tests are mocks.
- Module builds against both current and installed **test-kernel** headers;
  target vermagic, source-copy hashes and absence of autoload aliases checked.
  The target build is in `results/npu1-bootstrap-build/`; its source manifest
  records artifact hashes and explicitly says not loaded/installed/booted.

The lifecycle harness requires OpenSSL headers/libcrypto for its userspace
SHA-256 shim; the kernel module uses the kernel's exported `sha256()` function.

```sh
venv/bin/python scripts/test_linux_bootstrap.py
venv/bin/python scripts/test_linux_bootstrap_guards.py
```

#### Rebuilding the current bootstrap from source

Historical `results/npu1-*` directories are ignored local artifacts, not files
provided by a checkout. Build the current tracked source against the installed
test-kernel headers before the pending experiment:

```sh
make module KERNEL_RELEASE=7.2.8-23-omarchy-p16-asus-npu1
modinfo -F vermagic driver/n1x_npu_bootstrap.ko
modinfo -F alias driver/n1x_npu_bootstrap.ko
```

Vermagic must begin with that exact release, and alias output must be empty.
This produces `driver/n1x_npu_bootstrap.ko` plus the observer module. It neither
installs, loads nor triggers either module. The explicit target works while the
old kernel is running, provided the test headers are installed. With no override,
`make module` and `make kernel-check` use the running kernel as before. A later
build for another release replaces the in-tree module artifact; check vermagic
again before using it. Missing headers fail the build rather than falling back.
The bootstrap still refuses any kernel other than the exact audited npu1
release. Changing the Make target does not relax its runtime guard.

#### Hardware qualification sequence — still pending

First manually boot **N1X NPU test**, verify graphics/input and confirm the exact
uname above. Complete the earlier SROOT CCA/domain and allocation-only procedure
before considering an upload. Both observer bindings must be unloaded before
bootstrap can bind, and the per-device domain-selection script requires unbound
devices. Keep the working boot entry available. These commands are a pending
manual experiment, **not a record of execution**:

```sh
# Only after test-kernel boot and allocation-only qualification:
sudo rmmod n1x_npu_observe
# Copy the verified extracted vendor input, not the raw HAL-1 firmware image:
sudo install -Dm644 sources/dla-p16-sroot-v0.bin \
  /usr/lib/firmware/nvidia/n1x/p16-sroot-v0.bin
# Use the current source build above; confirm npu1 vermagic before loading:
sudo insmod driver/n1x_npu_bootstrap.ko \
  enable_upload=1 enable_ping=1 enable_region5=1 enable_config_snapshot=1 \
  enable_config_apply=1 enable_lpwr_config=1 enable_bypass_task=1
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
# This is the one write trigger; it requires retention/reboot afterward:
sudo python3 scripts/npu_trigger.py upload
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
sudo journalctl -k -b --no-pager
```

Do not loop/retry the trigger or free the retained buffer after a failed or
successful phase. Capture the actual reply, idle/error, IOMMU/kernel logs and
system stability before deciding the next bring-up step. The current machine
still runs the original kernel; no bootstrap module has been installed/loaded,
no firmware file copied to the system firmware path, and no NPU register written
by this work. The installed test UKI/menu/defaults remain unchanged.

### Opt-in DMA-word ping phase (built, not yet run)

The bootstrap module now also exposes a root/CAP_SYS_RAWIO `ping` attribute,
requiring immutable `enable_ping=1` at module load. It performs no automatic ping.
It requires a successful upload reply and idle observation from this same boot,
both endpoint bindings and the DLA DMA gate again. An unrelated later sysfs
error is not treated as evidence that the earlier upload did or did not succeed.
The phase can be attempted only once after its first device-state write.

The DLA-owned coherent allocation is 4096 bytes, nonzero and 256-byte aligned,
with its whole extent within the 36-bit DMA mask. It is cleared and its first
little-endian word initialized to `0x11111111`. Before submission the phase
reads route ownership `0xd2c` and mask `0xd28`, preserves both and clears only
`owned & mask` through `0xd24`, following the observed initial-loader policy.
Errors before that first mask-write attempt free the unpublished ping buffer.
After that point the buffer is retained on all outcomes under the upload's
existing module/runtime references. No automatic mask restoration/retry or
quiescence claim is made.

After `dma_wmb()`, the actual C method sender sends **method 1**, data = ping
IOVA >> 8, without response flags. This follows the now-executed original
Windows direct wrapper; its success check is the DMA word, not a mailbox reply.
The Linux loop uses READ_ONCE/little-endian conversion, followed by dma_rmb on
success. Its explicit Linux policy is at most 5001 word reads with 1000-us
sleeps between attempts. It does not infer Windows delay-helper units or copy
the original final-delay boundary. Lowest-sender errors propagate immediately.
A changed word `0x44444444` sets `ping_verified`; every other result, timeout or
send error leaves it false. Even success retains storage until reboot because
whole-device DMA quiescence is not established.

Only the additional logical DLA registers required by this phase are admitted:
reads `0x68`, `0xd28`, `0xd2c`; writes `0x68`, `0x6c`, `0xd24` (plus the earlier
read-only idle offset). They use the recovered Windows-1 remapper and bounds.
The ping phase itself sends no mailbox ACK; the optional control phase below
adds response handling. There is still no CPUCTL/reset write, OS IRQ handler,
full IRQ enable, region/configuration initialization, task submission or inference UAPI. The
success path of the raw-firmware model emits no response interrupt; live
interrupt behavior for the opaque loaded image still needs qualification.

The new status fields report ping enable/attempt/error, numeric word,
verification and retained-buffer presence. An explicitly permitted pending
manual test, **only after upload status is successful**, is:

```sh
printf 'ping\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/ping
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

Decide to enable the optional phase **before** the upload: after any upload
attempt the module cannot normally be unloaded to change its load parameters.
The earlier pending module-load example now includes both options and uses the
ping-capable build originally at `results/npu1-bootstrap-ping-build/`. The current
control-capable build below supersedes that artifact. Earlier upload-only and
ping-only artifacts are historical builds.

Validation: ten complete original Windows ping/direct-wrapper cases (six
completion boundaries and four failures), original reset/trap/DMA firmware runs
without function stubs on modeled completion cases; 29 exact Linux ping-function
lifecycle cases linked to the real C method sender; 21 prior upload cases; and
2592 DMA/MMIO guard cases plus machine/resource-end controls. Both kernel builds
pass after correcting a collision with the kernel `current` macro. Target
vermagic, source-copy hashes and no aliases checked. The new manifest records
the initial failed build and corrected successful build. No module was loaded,
no firmware installed and no physical ping/upload performed. These tests validate
software contracts under mocks; hardware boot and inference remain open.

### Optional command-22 configuration export (built, not yet run)

The bootstrap module now offers a third immutable option,
`enable_config_snapshot=1`, and root/CAP_SYS_RAWIO trigger `read_config` accepting
`read`. It requires both endpoint bindings and this boot's **verified DMA-word
ping**. There is no automatic export. The separate optional default-import
phase below adds an explicit write trigger; arbitrary settings are not exposed. The updated pending module-load command above enables all three
independently triggered phases; select options before the first upload since
module/runtime references are retained afterward.

The snapshot phase validates DLA's translated/coherent DMA gate again, allocates
its own zeroed 4096-byte coherent buffer, and checks nonzero 256-byte alignment
and the entire 36-bit address extent. It issues command 22 through the new
`n1x_dla_control_command` pipeline. This pipeline prechecks for an outstanding
SWGEN1 reply, sends the response-enabled control method, polls, captures/clears/
ACKs, and requires matching command/address and zero status/upper byte before
accepting message 2. Unlike the original ping's word-only path, this phase
explicitly requests a mailbox response.

The SROOT upload and ping buffers are separate and remain retained. The config
buffer is also retained after any control-transaction attempt, even stale-reply
or pre-send failures. Only allocation/range failures before the transaction can
free it. After a successful matched response, dma_rmb precedes copying the first
252 raw bytes into a separate CPU snapshot. Status reports attempt, last error,
raw reply/validity and snapshot validity. `config_snapshot` is root-readable
hexadecimal (504 hex digits plus newline) only after success; prior reads return
ENODATA and never fetch hardware. No field names, physical units or atomicity
of the firmware's internal snapshot are inferred.

The required additional logical DLA register accesses are status/mailbox reads
`8/0x40/0x44` and IRQ-clear/mailbox writes `4/0x40`, alongside the existing
restricted mask/method callbacks. No CPUCTL/reset register, OS IRQ handler or
GSI-role assumption is added. Firmware rearming SWGEN1 and physical IRQ delivery
for the opaque SROOT image remain unqualified. The optional manual sequence is
therefore still a pending bring-up experiment, not production configuration:

```sh
# Only after this boot's upload and DMA-word ping status are successful:
printf 'read\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/read_config
sudo cat /sys/bus/platform/devices/NVDA200A:00/config_snapshot
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

`test_linux_config_snapshot.py` runs the exact Linux snapshot function linked
to the actual control/sender/response C pipeline for 26 lifecycle cases. It
checks disabled/failed-ping gating, wrong DMA extent, all ten I/O failures,
stale/rejected/mismatched responses, prevention of repeat attempts, early-only
freeing, and no publication of failed snapshots. The independent original
firmware control test checks all five supported control commands through mret.
The 21 upload, 29 ping and 2592 DMA/MMIO cases still pass. These are mocks of
OS/device effects, not physical tests.

Both kernel module builds pass. Current source/artifact hashes and target
vermagic are checked in `results/npu1-bootstrap-control-build/source-manifest.json`.
Earlier upload-only/ping-only build directories describe earlier sources, not
this current module. No bootstrap has been loaded, no firmware installed and no
upload, ping or config read has occurred on the machine. Full initialization,
IRQ routing/recovery and inference remain incomplete.


### Optional exact-layout region-5 setup (built, not yet run)

`enable_region5=1` adds a root/CAP_SYS_RAWIO `init_region5` trigger accepting
`init`, gated on this boot's verified DMA-word ping and both endpoint bindings.
It initializes the exact Windows-default 1-MiB allocation/layout through command
11, preserves the buffer after every transaction attempt, and reports attempt,
error, registration and retention in status. The region phase itself does not implement ring production/consumption, task
submission or configuration import; default import is a separate step below.

The recommended pending qualification order is upload, DMA-word ping, region-5
registration, then optional configuration export. The earlier config-only
experiment can still run without region 5; its export does not import/mutate
operating settings. The updated pending module-load example selects all four
independently triggered phases and uses the latest source-matched module at
`results/npu1-bootstrap-region5-build/`. Historical control-only and ping-only
builds do not contain this phase.

```sh
# Only after upload and DMA-word ping status are successful:
printf 'init\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/init_region5
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

See [region-5 allocation and initialization evidence](region5.md) for the original
constructor/provider/builder functions, exact arithmetic, buffer comparison,
failure policy, firmware test and remaining ring semantics. The 26 new lifecycle
cases and prior 26 snapshot, 29 ping, 21 upload and 2592 DMA/MMIO cases pass.
Both current- and test-kernel module builds pass. No module was installed/loaded,
no firmware file installed and no device register touched by this work.


### Optional Windows-default configuration import (built, not yet run)

The original `0xd47830` modifier changes bytes 1->2 and 50->1 even without an
active override table. The C helper and native import phase now reproduce
that default transformation, gated on ping, registered region 5 and a valid
prior export. Immutable `enable_config_apply=1` permits the root/CAP_SYS_RAWIO
`apply_config` trigger (`apply`). It never imports automatically and accepts
no arbitrary payload or custom settings.

The already retained config DMA allocation is reused after gate/extent checks;
the original saved snapshot remains pre-import data. After dma_wmb, command 21
uses the checked control/ACK pipeline. Acceptance is reported separately from
actual hardware readback/full initialization, and all buffers remain retained.
No native persistence/readback verification is claimed. See
[default-config evidence](default-config.md) for the six full Windows/C
transform comparisons, same-instance 22/21/22 firmware test and 27 Linux
lifecycle cases.

The updated pending manual sequence is upload → DMA-word ping → region 5 →
config export → default config apply. Select all needed options before upload.
The module-load example above now uses
`results/npu1-bootstrap-bypass-build/`; earlier region/control-only builds
remain historical artifacts. Both kernel builds and affected regressions pass.
No bootstrap has been loaded, firmware installed or physical import performed.
The optional low-power configuration import is described below. IRQ
initialization, recovery, a usable task interface and numerical inference
still require implementation/qualification.


### Optional Windows-fallback LPWR configuration import (built, not yet run)

`enable_lpwr_config=1` permits root/CAP_SYS_RAWIO to write `apply` to
`apply_lpwr`, once, after a verified DMA ping, registered region 5 and accepted
command-21 default config import. No automatic submission occurs at probe.
The payload uses the original Windows constructor inputs `+47d=1`, `+490=1`
and the **zero-provider fallback** `+f4=4`: byte 0=1, byte 1=1, LE u32 at
byte 2=8000, remaining bytes zero. This is an explicit fallback experiment,
not evidence that a live Windows session on this P16 would use that value.
No physical time unit is assigned. No arbitrary user tuning is exposed.

The phase rechecks the DLA DMA gate, obtains a new coherent 4096-byte allocation,
checks its entire nonzero/256-aligned extent against the 36-bit mask and clears
all backing. The recovered C builder fills 40 bytes, followed by dma_wmb and
checked command 23. Before any control transaction the attempt flag is set;
all later outcomes retain backing under the existing upload/module/runtime
references. Only pre-publication preparation failures free the new buffer.
`status` records enabled/attempted/error/accepted, raw reply validity/header
and retention. Acceptance does not mean full initialization, a working IRQ
path, measured power control or inference. No native command-24 readback is
performed by this phase.

```sh
# Only after the preceding phases succeeded in the pending hardware experiment:
printf 'apply\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/apply_lpwr
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

`scripts/test_linux_lpwr_config.py` compiles the exact native phase with the
real C builder/control/send/ACK functions. It covers 28 lifecycle cases:
prerequisite failures, DMA gate/mask/allocation failures, whole-extent checks,
all ten transaction I/O failure positions, stale replies, firmware errors,
response mismatch, success and repeated attempts. The whole submitted 4096
bytes match the original Windows ARM64 builder with fallback inputs, including
its cleared tail. A separate same-instance original raw HAL-1 23/24 roundtrip
clears host memory between commands, executes 20 original modeled DMA words
and returns through both original traps, without firmware code patches/stubs.
This is emulation with synthetic reset/DMA/IRQ effects, not physical validation
or proof that the opaque SROOT image executes identical code.

Both kernel module builds pass; the npu1 artifact and source manifest are in
`results/npu1-bootstrap-bypass-build/`. No module or firmware has been installed
by this addition, and no LPWR command has been sent to hardware.


### Single-task bypass descriptor for completion qualification

`n1x_dla_build_bypass_fenced_task_v2()` adds the recovered byte-41 flag bit 0
to the fenced descriptor and rejects task chains. Original raw HAL-1 firmware
now runs this descriptor from reset/trap through scheduler success, terminal
fence write, dequeue and SWGEN0 in emulation. The same invalid zero network
fails with the flag removed. A malformed postaction writes the fence but
subsequently reports failure without SWGEN0, preventing the inference that
fence equality alone means success. See [exact path and tests](bypass-task.md).
The bootstrap now offers a root-only one-attempt trigger for this fixed
descriptor, described below; it has no arbitrary model/task UAPI. Physical IRQ association, error/recovery ownership and numerical
execution remain open.


### Native fixed-descriptor task qualification (built, not yet run)

The optional `enable_bypass_task=1` / root `bypass_task` (`run`) phase now
connects the recovered serializer to the actual bootstrap allocator, sender,
mailbox polling and DMA fence observation. It requires accepted config and
LPWR imports, rejects preexisting completion IRQs and admits exactly one own
4096-byte DMA allocation. Acceptance, errors, fence and SWGEN0 are reported
separately; all backing survives attempted submission until reboot. No model
pointer or arbitrary descriptor is accepted from userspace.

```sh
# Only after all preceding phases in the pending hardware qualification succeeded:
printf 'run\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/bypass_task
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

The exact native phase passes 44 lifecycle cases, including all 14 I/O fault
positions and a late postaction error after fence/SWGEN0 observation. See
[buffer layout, poll/error policy and test limits](bypass-task.md#native-one-attempt-bootstrap-phase).
The newest target artifact is `results/npu1-bootstrap-bypass-build/`; no
bootstrap module/firmware installation or physical submission occurred.
This adds a fixed transport qualification operation, not a working inference
UAPI, OS IRQ driver, recovery path or numerical computation.
