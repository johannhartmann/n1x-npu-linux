# Early board-container capture on the booted npu2 kernel

The capture-OFF npu2 kernel physically booted on 2026-10-27. The next manual
entry is **N1X NPU FRTS capture - npu2 with disk log**. It uses exactly the
same kernel and initramfs as that successful boot, with only two added options:

```text
log_buf_len=4M n1x_frts_snapshot=p16-bios300
```

The 4 MiB printk buffer addresses the truncated backlog observed in the first
physical disk log. It retains messages for the initramfs logger; it does not
provide persistent storage before initramfs starts. The bounded, read-only
capture still follows the [EFI/RAM guards](hfrp-vbios.md#capture-implementation).
No NPU driver binding, firmware upload, reset or device-register write is added.

The target is a possible firmware-provided GFWI/VBIOS container with board
policy needed by the reconstructed initialization sequence. Its presence at
the CPU physical candidate remains unproven. This is not an inference test or
evidence that the NPU firmware is running.

## Reproduce preparation and verification

```sh
venv/bin/python scripts/prepare_npu2_capture.py \
  results/npu2-uki results/linux-7.2.8-p16-npu2 results/npu2-frts-capture
python scripts/test_npu2_capture.py
python scripts/test_frts_snapshot.py
python scripts/test_bootlog_vm.py \
  results/npu2-frts-capture/frts-capture.efi \
  results/linux-7.2.8-p16-npu2/arch/arm64/boot/Image \
  results/qemu-efi/usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  results/npu2-frts-capture-vm --frts-fixture results/frts-qemu-fixture.gfwi
pkexec /usr/bin/python3 /home/johann/n1x-lab/npu/scripts/install_npu2_capture.py \
  /home/johann/n1x-lab/npu/results/npu2-frts-capture \
  /home/johann/n1x-lab/npu/results/linux-7.2.8-p16-npu2 \
  /home/johann/n1x-lab/npu/results/npu2-frts-capture-vm/result.json
```

Use new output directories for fresh runs. Preparation preserves every PE
section other than `.cmdline` and verifies the kernel, initramfs boot files,
ACPI tables and release. The installer checks the current npu2 options, installed
Image/BTF, both fallback image checksums and the matching successful VM result.
It adds an entry without changing the existing default or timeout, and never
reboots. The checkpoint is `/var/lib/n1x-npu-test/npu2-frts-capture`.

On 2026-10-27, the VM test recovered all 36864 synthetic fixture bytes from
sysfs and the virtual SSD, with the exact fixture SHA256
`83bfc9c3c42ee137579f9f30c2c48ba99d3cc74d019e83c364160d7a4836ed65`.
The persistent 276137-byte log includes the early capture message and kernel
banner despite `initcall_debug`. No module-BTF rejection occurred. This tests
the implemented path in an isolated VM, not the physical RAM candidate.

Installed capture UKI SHA256:
`3336422279d9353b513c2347fdc82d230ad952dbf529b76b8478159de5244cf7`.
Kernel Image SHA256 remains
`a4a6bfca3fcab0fb6bff10a9c0232113fdbc88b31830c965f807187b961f0235`.
The physical capture remains pending.

The old, already unreferenced first diagnostic image was checksum-verified and
archived at `/var/lib/n1x-npu-test/frts-diagnostic/retired-diagnostic.efi` to make
EFI space. Both the stock default and the successfully booted npu2 capture-OFF
image remain available.

## After the manual boot

Check the new boot ID, command line and `/data/n1x-bootlogs/<boot-id>.log` for
`n1x-frts:`. If `/sys/firmware/n1x_frts_snapshot` exists, save it **before another
reboot**; the kernel-owned snapshot is volatile. Read the full file rather
than assuming a single sysfs read returns 36 KiB. The existing
`scripts/extract_gfwi_vbios.py` validates member bounds and extracts the ROM
from the acquired container. Board identity and BIT token `l` policy must then
be validated before attempting an NPU bootstrap.

If no snapshot is published, preserve the exact rejection message and stop
this acquisition path for analysis. A missing candidate is not a reason to
scan unrelated physical memory or upload guessed board policy.
For a failed boot, return to **Omarchy/linux-p16-asus-23** or
**N1X NPU diagnostics - disk log (capture OFF)** and retain the SSD log and
last visible screen. A hang before initramfs may still leave no disk log.
