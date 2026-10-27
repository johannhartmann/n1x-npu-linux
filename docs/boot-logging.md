# Diagnostic boot logging on the P16

The old early-capture and capture-OFF UKIs must not be retried. An initramfs VM
test exposed an additional packaging defect: modules
copied from the original npu1 kernel have split BTF metadata referring to a
different vmlinux. The kernel rejects them, including `nvme_auth`, `hid`, and
`i2c_mt65xx`. Matching release names and exported symbol CRCs did not catch this.
This is a confirmed defect, but it does not establish the cause of the last
visible frame at 0.309 seconds on the physical machine.

The invalid capture-OFF menu entry has been removed. Its EFI file remains
archived in place, and the menu backup is
`/var/lib/n1x-npu-test/frts-diagnostic-keep-console/limine.conf.before-btf-retirement`.
The normal `Omarchy/linux-p16-asus-23` default and five-second timeout remain.
Do not restore that diagnostic entry to retry the same image.

## What is captured

`scripts/n1x_bootlog.c` records `/dev/kmsg`, including the available kernel
backlog, into `/data/n1x-bootlogs/<boot-id>.log`. Files are root-readable only.
The recorder flushes its file at least once per monotonic second, subject to
the kernel and storage device making progress. It stops after 600 seconds or
8 MiB of records, plus its small header/trailer. A clean termination writes
`N1X_BOOTLOG_END`; its absence can indicate interruption or an I/O failure.
The last unflushed messages can be lost in a hard reset.

The opt-in `n1x_bootlog=1` initramfs hook starts immediately after the udev early
hook, before the regular `MODULES` list (including graphics). It loads NVMe,
waits up to ten seconds for the audited unencrypted ext4 volume UUID
`e4b5f9f3-205f-469f-a75d-a09a67786a92`, and mounts it at
`/run/n1x-bootlog-data`. It neither formats nor runs filesystem repair. Failure
to find/mount the disk disables logging rather than making it a boot requirement.
The cleanup hook stops the recorder and attempts to unmount before switching
to the normal root filesystem. This is an early-boot recorder, not a permanent
service for the subsequent desktop session.

**A hang before initramfs userspace starts is not covered.** Keeping the EFI
boot console (`earlycon=efifb keep_bootcon`) preserves visible messages; it does
not persist them to disk. An external UART recording or a separately qualified
RAM-retention logger is needed to cover that earlier interval.

`systemd-pstore.service` is enabled on this machine to archive existing pstore
records under `/var/lib/systemd/pstore`. The loaded EFI pstore backend is a
dump backend, not a continuously recorded console. It is not evidence that a
silent hang will produce a record. No panic, EFI-variable stress test, or
physical power-cycle retention test was performed.

## Build and test without rebooting the host

```sh
python scripts/build_logged_initramfs.py \
  results/frts-early-v1 results/frts-diagnostic-keep-console \
  results/frts-logged-initramfs-new

python scripts/test_bootlog_vm.py \
  results/frts-logged-initramfs-new/linux-p16-asus-23-npu1.efi \
  results/frts-logged-initramfs-new/kernel/arch/arm64/boot/Image \
  results/qemu-efi/usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  results/bootlog-vm-new
```

The builder copies the kernel tree and builds Image and modules together. Only
the staged module root is changed; mkinitcpio runs with `--nopost`. The real UKI
is checked for kernel, command line, ACPI overrides, boot files, logger binary,
hook contents and early-hook order. The scripts do not install it.

The VM uses TCG, the actual staged initramfs and kernel, and a new 128 MiB
file-backed NVMe. It attaches no host disk and has no network device. It writes
a unique test marker, invokes the cleanup hook, powers off the guest, then reads
the log from the virtual disk with debugfs. This checks persistence through
guest shutdown, not through loss of power on the physical P16.

The live disk probe succeeded: `/data/n1x-bootlogs/probe-result.json` records a
249465-byte log containing both the injected marker and a clean-exit trailer.
That test exercised the actual SSD from the running kernel, not early boot.
The first VM test (`results/bootlog-vm-v1`) failed because of the stale module
BTF and produced no disk log. Keep that failure as evidence, not a passing test.

The corrected test (`results/bootlog-vm-v2/result.json`) passed with the staged
build in `results/frts-logged-initramfs-v3`: 54926 bytes recovered after guest
shutdown, containing kernel backlog, the injected marker and a successful
recorder trailer. Its serial log contains no module-BTF validation rejection.
UKI SHA256 is
`6874a80e839f3c740522dff41079ce4505d439c5ec9c355e38803b6867adcbdc`;
kernel Image SHA256 is
`c403fec071ea3be372b0d7b71c045bd6ab0fe33f7fcccaa716b8cc2f9de458f7`.
The build remains outside `/boot`. No physical reboot has qualified it.

## Deployment boundary

The corrected initramfs alone is insufficient for normal boot: after switching
root, later modules must match the same kernel too. Existing UKI-only installers
now reject a BTF mismatch against the installed kernel build. Before another
physical test, the corrected kernel and complete module set must be packaged
under a separate release, including matching external modules, and the logging
path qualified. The `npu2` procedure below does this without replacing the
original npu1 module tree or the working default.

This logging implementation does not guarantee a recoverable log
from the originally photographed early-boot failure.

## Separate npu2 package and boot entry

`7.2.8-23-omarchy-p16-asus-npu2` retains the reviewed configuration and patches
from the corrected source and changes its release suffix. Kernel Image and
in-tree modules are built together. NVIDIA 615.78.08, spbm
0.3.0.rtxspark.5 and cpuid 0.8.2 are rebuilt against that same tree. The
observation module is installed for explicit later use, not included in the
initramfs. The FRTS capture parameter remains absent and no firmware upload is
triggered by this installation.

The existing scripts support both the original npu1 and the new npu2 release;
no general kernel packaging framework is introduced. Use new output directories
for a fresh build:

```sh
python scripts/build_logged_kernel.py \
  results/frts-logged-initramfs-v3 results/linux-7.2.8-p16-npu2
python scripts/package_sroot_kernel.py \
  results/linux-7.2.8-p16-npu2 /home/johann/p16rtx_linux/kernel/PKGBUILD \
  results/npu2-kernel-packages
python scripts/build_sroot_external_modules.py \
  results/linux-7.2.8-p16-npu2 results/npu2-external-modules --jobs 8
python scripts/build_sroot_uki.py \
  results/linux-7.2.8-p16-npu2 results/npu2-kernel-packages \
  results/npu2-external-modules results/npu2-uki \
  --bootlog-cmdline results/frts-logged-initramfs-v3/cmdline
python scripts/test_bootlog_vm.py \
  results/npu2-uki/linux-p16-asus-23-npu2.efi \
  results/linux-7.2.8-p16-npu2/arch/arm64/boot/Image \
  results/qemu-efi/usr/share/qemu-efi-aarch64/QEMU_EFI.fd results/npu2-bootlog-vm
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/install_sroot_test.py \
  /home/johann/n1x-lab/npu/results/npu2-uki \
  /home/johann/n1x-lab/npu/results/linux-7.2.8-p16-npu2 \
  /home/johann/n1x-lab/npu/results/npu2-kernel-packages \
  /home/johann/n1x-lab/npu/results/npu2-external-modules \
  --release 7.2.8-23-omarchy-p16-asus-npu2 \
  --vm-result /home/johann/n1x-lab/npu/results/npu2-bootlog-vm/result.json
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/audit_sroot_test.py \
  --release 7.2.8-23-omarchy-p16-asus-npu2 \
  --uki-directory /home/johann/n1x-lab/npu/results/npu2-uki
```

Installation requires the matching VM result, package checksums, host-config
snapshot, root-module BTF and a valid working fallback. Package hooks are masked
only for this explicit transaction, then the verified UKI is added manually.
The entry is **N1X NPU diagnostics - disk log (capture OFF)**. It is not made
the default, and the installer never reboots. Checkpoint and original menu are
under `/var/lib/n1x-npu-test/npu2`.

The npu2 procedure completed on 2026-10-26. Both pacman packages are installed
as version `7.2.8-23`. All seven rebuilt external modules plus the explicit-load
observation module were verified after installation. Packaged vmlinux BTF
matches the build. The final read-only audit is
`results/npu2-installed-audit.json`; it confirms the installed UKI, module
dependencies, unchanged menu defaults and valid working fallback digest.
The capture-OFF kernel physically booted on 2026-10-27. The live result is
`results/npu2-live-boot.json`, boot ID
`6b58f0e3-76c3-447c-af4f-ca3a8b0c9aaa`. NVIDIA 615.78.08 is active and
systemd reports no failed system services. No module-BTF rejection, invalid
module format, unknown symbol, version-magic rejection or NVIDIA Xid was found
in the saved log/current kernel journal. Existing ACPI, ASUS handshake and
touchpad feature-query warnings also appeared in the preceding stock-kernel
boot; this result does not establish that every device feature works.

The actual SSD log is
`/data/n1x-bootlogs/6b58f0e3-76c3-447c-af4f-ca3a8b0c9aaa.log`, 340180 bytes,
SHA256 `aea915e848d183e6472f945f491843014ec7b79a02e838db4a6b20d002575997`.
It ends with `N1X_BOOTLOG_END: bytes=340059 error=0 stopped=1`. Its first
record is sequence 435 at 7.879909 seconds; the kernel version banner is
already absent from the finite printk backlog. Its last kernel record is at
83.160116 seconds during root Btrfs setup. Logging from initramfs to the real
SSD is now physically verified, but the early backlog is incomplete. Increase
the printk buffer for a subsequent diagnostic capture before relying on it
to retain the earliest messages. No hard-reset/power-loss test was performed.
FRTS capture was disabled, and no NPU firmware boot is implied. The profile
had returned to balanced after reboot and was restored to performance.

The VM recovered a 54859-byte disk log after guest shutdown with the kernel
backlog, injected marker and clean recorder trailer; no module-BTF rejection
was present. Results are in `results/npu2-bootlog-vm/result.json`. UKI SHA256:
`9e04205026c23021d15d06d8ecf74d5f72ecd06f9ee9a9ffc37617dff74cf8f4`.
Kernel Image SHA256:
`a4a6bfca3fcab0fb6bff10a9c0232113fdbc88b31830c965f807187b961f0235`.

The unreferenced `-keep-console.efi` was copied, checksum-checked and archived
at `/var/lib/n1x-npu-test/frts-diagnostic-keep-console/retired-diagnostic.efi`
before removing its EFI-partition copy to make space. No referenced boot image
was removed.

After a physical test, return to `Omarchy/linux-p16-asus-23` if necessary and
read `/data/n1x-bootlogs/` with root permissions. A new file proves that the
logger reached userspace; no file does not identify why boot stopped. Preserve
a photo of the last visible messages as well. Physical boot success must be
recorded separately from the VM/package checks.
