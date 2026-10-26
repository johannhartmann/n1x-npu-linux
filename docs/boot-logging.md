# Diagnostic boot logging on the P16

The early-capture and capture-OFF UKIs are **not qualified for another physical
boot**. An initramfs VM test exposed an additional packaging defect: modules
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
physical test, package the corrected kernel and complete module set under a
separate release, including matching external modules, and qualify the logging
path. Do not replace the original npu1 module tree or the working default.

Current logging sources and staged builds do not guarantee a recoverable log
from the originally photographed early-boot failure.
