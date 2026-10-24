# NPU boot provider: SROOT mailbox

The default NPU boot path reaches **nvsrootmbx.sys**, not merely another entry
point inside nvlddmkm. This is established by matching interface registration
and IOCTL records, not by guessing from driver names.

## Verified chain

1. nvlddmkm RVA `0xd3f774` calls `0xd3b008` and stores the result in outer
   device +0x308. That opener calls IoGetDeviceInterfaces with GUID
   `94f86b15-22a9-4607-b95b-3b05184ae8f7`, then RtlInitUnicodeString and
   IoGetDeviceObjectPointer. Import identities are read from its PE table.
2. The default method-2 wrapper `0xd49ef0` passes this handle to `0xd3bbc8`.
   Its normal request is IOCTL `0x150214`, with the firmware blob as input.
   Subtype 1 sends `0x150218` and the eight-byte reinitialization request.
3. The recovery image contains two GUID matches, nvSCP.sys and nvsrootmbx.sys.
   A GUID occurrence alone does not identify which is a provider.
4. nvsrootmbx at RVA `0x30d00..0x30d70` creates and enables this exact GUID through
   KMDF table indices 77/78. The original slice runs in the emulator with fake
   WDF handles/callbacks. Success sets its local enabled flag; simulated create
   failure skips enable and leaves the flag clear.
5. Its DMF IOCTL table records at RVAs `0x1b090` and `0x1b0b0` describe
   `0x150214` (minimum input 32 bytes) and `0x150218` (minimum input 0 bytes).
   Both point to handler RVA `0x321e0`. The latter's branch comparisons match
   both IOCTLs. The table has zero minimum output sizes. These are framework
   minimums, not proof of complete payload validity.

Microsoft's primary headers identify
[KMDF indices 77/78](https://github.com/microsoft/Windows-Driver-Frameworks/blob/main/src/publicinc/wdf/kmdf/1.33/wdffuncenum.h)
and the
[DMF IOCTL record layout](https://github.com/microsoft/DMF/blob/master/Dmf/Modules.Library/Dmf_IoctlHandler.h).
The examined binaries contain the DMF IoctlHandler source-path/type strings.

## Pinned vendor inputs

These were extracted without running Windows executables from the existing
ASUS recovery `sources/wim/boot.wim`. Binaries remain in ignored sources/.

| File | Version | SHA-256 |
| --- | --- | --- |
| nvsrootmbx.sys | 2.10.0.0 | f814e861b5e076aa4e00619a8a588df3c4ae9ec2b5026c30748ba493ce0e6d8a |
| nvSCP.sys | 2.17.0.0 | d1cc1ae1a907d607ad979e84116a5febdb2f55136b612b210c68fdd57f901a47 |

INF matches are `ACPI\NVDA8600` for nvsrootmbx and `ACPI\NVDA8200` for nvSCP.
`inspect_auxiliary_drivers.py` checks hashes, binary versions, GUIDs, IOCTL
records and the actual provider-registration slice, including its failure path.
The nvSCP match remains a reference/consumer candidate; its role is not inferred
solely from containing the GUID.

Reproduce extraction using the bundled unpacker (not the vendor installer):

```sh
tools/usr/lib/7zip/7z x -y -osources/platform-drivers sources/wim/boot.wim \
  'Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/*' \
  'Windows/System32/DriverStore/FileRepository/nvscp.inf_arm64_d79e80462fadf1d5/*'
venv/bin/python scripts/inspect_auxiliary_drivers.py
```

## Linux target and remaining work

Live read-only sysfs exposes `NVDA8600:00` at `\_SB_.ROT0` and `NVDA8200:00`;
neither had a platform driver link during inspection. Candidate archived ACPI
ROT0 has two overlapping resources, `0x18010000/0x1000` and
`0x18010000/0x10`. This is the mailbox device, separate from the NPU's
`0x19000000` resource. Do not assume its two resources are independent windows.

No drop-in Linux SROOT implementation was identified in the local kernel source
or the initial upstream search. The NVIDIA NV-Kernels power_wrap/SSPM work is
not established as a SROOT transport implementation. A broad recursive upstream
tree query was truncated and cannot prove absence across that repository.

Next: trace handler `0x321e0` into SendFwImageToSroot and NotifySrootReinitFw,
recover the mailbox request/completion protocol, payload lengths and timeout/error
behavior, and compare any Linux candidate with those observations. No mailbox
MMIO, IRP submission, firmware loading or reset was performed. Any eventual
ROT0 binding also requires live AML/power-path audit; the existing DLA0 audit
must not be treated as covering an unexamined auxiliary device.
