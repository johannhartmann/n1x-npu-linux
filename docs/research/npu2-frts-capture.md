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
## Physical result, 2026-10-27

The capture-enabled npu2 kernel booted successfully, boot ID
`6c8ae50e-4de7-4b42-b5ca-24b896556b8b`. The early function ran and reported:

```text
n1x-frts: candidate has no bounded GFWI ROM member
```

No `/sys/firmware/n1x_frts_snapshot` file was published. The message means
the EFI/unreserved ordinary-RAM guards and mapping succeeded, but either
the GFWI magic or its ROM-member bounds failed. The implementation does not
export invalid header bytes, so this result does not distinguish those causes.
Do not describe it as a dump of zero RAM or proof that no GFWI exists elsewhere.

The SSD log was saved locally under
`results/physical-frts-6c8ae50e-4de7-4b42-b5ca-24b896556b8b/`:
371080 bytes, SHA256
`cec49de233989ae3482665b565190b4bf497ea35cc9fd006402e9c4c4ab39b1c`.
It starts at printk sequence 0 and includes the kernel banner, the capture
rejection at sequence 11, and later SWIOTLB initialization. Its trailer is
`N1X_BOOTLOG_END: bytes=370959 error=0 stopped=1`; no module-BTF rejection
was found. The larger buffer therefore fixes the observed early-backlog loss.
It still cannot persist a log if a future kernel hangs before initramfs.

A fresh supported component-9 diagnostic query returned 250 bytes and still
reports scratch61/62/63 = `0xfc000000` / `0` / `0x10009`, with no raw-ROM
field. Files: `results/npu2-vbios-component9-20261027.pb` and its decoded JSON.
Thus the old scratch result did not simply disappear after reboot. This does
not establish what those registers held before the Linux GPU driver started.

The reference NVIDIA 615.78.08 source provides an important restriction:
`kfspFrtsSysmemLocationProgram_GH100` and `_GB100` obtain the scratch address
using `memdescGetPhysAddr(..., AT_GPU, 0)`. It is a GPU address in these paths,
not proof of CPU-physical identity. Furthermore, the generated host dispatch in
`g_kern_fsp_nvoc.c` does not select either implementation for GB20B; its default
`kfspFrtsSysmemLocationProgram_395e98` returns `NV_ERR_NOT_SUPPORTED`. These
reference functions do not establish the writer, timing or translation of the
observed GB20B registers. No register-programming function was executed.

This test rejects the specific hypothesis that a bounded GFWI ROM member
could be recovered at CPU physical `0xfc000000` at this capture point. Later
SWIOTLB clearing alone cannot explain this negative result, since the rejection
preceded it. Do not repeat this boot unchanged or scan alternative physical
addresses. Continue by establishing the GB20B address-space/producer semantics
or obtaining a bounded export of the firmware-owned ROM. NPU firmware boot
and inference remain unverified.

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
