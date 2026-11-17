# Startup diagnostics and recovery boundaries

The driver identifies supported hardware through ACPI IDs, `_HRV`, resources,
DMA constraints and firmware protocols. It has no ASUS model or BIOS-version
allowlist. Passing those checks does not prove that the platform's HFRP peer
is operational. In particular, an accepted firmware stamp or mode value does
not imply reset completion.

## Diagnosing a failed start

The cached `hfrp_status` debugfs file records the last HFRP preparation stage:

| `stage` | Operation being attempted, or completed state |
| --- | --- |
| `not-started`, `endpoints` | No preparation yet, or missing bound endpoints |
| `dla-dma`, `sroot-dma` | Validate each endpoint's DMA/IOMMU contract |
| `vbios-policy` | Locate, parse and validate the board's VBIOS policy |
| `runtime-pm` | Acquire the DLA runtime-PM reference |
| `stamp` | Read and check the HFRP firmware stamp |
| `module-reference` | Acquire the publication lifetime reference |
| `handshake` | Select circular mode, request reset, wait for ready and exchange the hello packet |
| `version`, `policy`, `power-on` | Discover the protocol, configure policy and request power |
| `ready` | HFRP preparation completed; later SROOT/DLA stages have separate status fields |

`initial_stamp_valid` distinguishes an actual early stamp observation from a
failed or unattempted read. `initial_stamp` survives a handshake or version
discovery failure. The existing `stamp` field belongs to version discovery
and may still be zero when the initial handshake fails.

The handshake has a separate retained result; version, policy, power and
diagnostic queries cannot overwrite it. Important fields are:

- `mode_select_attempted`, `mode_write_completed`: the mode write was attempted
  or returned success. This is **not** a mode-register readback.
- `reset_attempted`, `reset_completed`, `peer_ready`: whether reset was requested,
  its bit was observed clear, and the ready bit was observed set.
- `wait_offset`, `wait_valid`, `wait_value`, `wait_reads`: the last reset/ready
  poll, its last successfully observed value, and its successful read count.
  A failed first read has `wait_valid=0`; a later read error retains the last
  successful observation. A new wait starts a new observation record.
- `handshake_ack_completed`, `handshake_head_attempted`,
  `handshake_reply_valid`, `handshake_reply`: acknowledgment, publication and
  reply evidence from the initial handshake.

These values are recorded by existing accesses. Reading status performs no
MMIO, acknowledges no interrupt, issues no command and does not retry startup.
They describe observations, not a new hardware capability or an idle guarantee.
Checked shutdown/state reset clears them before the next startup generation.

For a device that is already bound, collect diagnostics without reloading it:

```sh
uname -a
journalctl -k -b --no-pager | grep -Ei 'n1x|sroot|hfrp|iommu|nvidia'
# If debugfs is already mounted, substitute the actual bound device name:
sudo cat /sys/kernel/debug/n1x-npu-bootstrap/NVDA200A:00/hfrp_status
sudo cat /sys/kernel/debug/n1x-npu-bootstrap/NVDA200A:00/status
sudo cat /sys/kernel/debug/n1x-npu-bootstrap/NVDA200A:00/shutdown_status
```

Do not write `shutdown` or `query_power_state` merely to collect this report.
ACPI resource assignments and probe order are also available through
the kernel's dynamic-debug probe message. That message uses resource metadata;
it does not inspect additional hardware registers. A useful cross-board report
includes the exact source revision, Linux/NVIDIA versions, ACPI tables, firmware
hashes and boot history. BIOS version is comparison metadata, not a diagnosed
cause or an instruction to update firmware.

## Surface issue #30 and the Windows reference

[Issue #30](https://github.com/johannhartmann/n1x-npu-linux/issues/30)
reports an N1X Surface whose HFRP reset request is not consumed and whose ready
bit does not appear. Failure occurs before SROOT firmware upload. An identical
SROOT image hash therefore does not establish that this earlier dependency works.

The portable regression uses the reported stamp `0x20251722`, initial mode 1,
and register words `0x683` / `0x05900000`, with explicitly synthetic ring noise.
It executes the real Linux C handshake against a nonresponsive simulated peer:
5,001 reset reads exhaust the poll bound, no hello command is published and
the failed channel rejects further work without I/O. This reproduces software
handling of the symptom, **not its physical cause or a Surface fix**. Scheduling
and the real delay implementation determine wall-clock duration.

The pinned Windows 617.42 `nvlddmkm.sys` has SHA-256
`73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987`.
A targeted radare2 disassembly confirms an important distinction at handshake
RVA `0xd4af88`: the first poll result is compared with Windows status `0x65`
at VA `0x140d4b038`; the timeout path logs and then continues into the next
poll at `0x140d4b04c`. The second poll's timeout path instead returns `0x65`
at `0x140d4b088`. Reproduce with the verified vendor input:

```sh
sha256sum sources/nvidia/Display.Driver/nvlddmkm.sys
r2 -e bin.strings=false -e scr.color=false -q \
  -c 'pd 100 @ 0x140d4af88' sources/nvidia/Display.Driver/nvlddmkm.sys
```

The earlier [original-code replay](https://github.com/johannhartmann/n1x-npu-linux/blob/597ae9d6afa836fb26ab3ea3edf5354bf3ae0be6/docs/hfrp-irq.md#complete-original-handshake-in-the-emulator)
also observed the ignored initial reset timeout. Linux deliberately rejects
that timeout. Ignoring it would not resolve the reported missing ready bit.
The current [offline vendor replay](vendor-replay.md) additionally executes the
original global initializer, HFRP wrapper, mode selector and version discovery.
It demonstrates that the replayed Windows outer initializer can return success
with no HFRP context, with HFRP mode 0, or after an early handshake failure;
the original no-transport power branch executes, while the loader remains
modeled. This is vendor error-propagation evidence, not an observed Windows
device or inference run. Device Manager success alone would consequently not
prove a working DLA transport. This evidence still does not identify who starts
the HFRP peer on the Surface.

The recovered Windows engine-reset path sends HFRP `RESET_IDLE`, `RESET`,
then `RESET_IDLE` before loading firmware. The
[transport replay](https://github.com/johannhartmann/n1x-npu-linux/blob/179effb2129efdd8aa0ec9a6e8087b210bdebdf7/docs/windows-kmd-task.md#reset-requests-through-the-original-hfrp-transport)
found that upper layers ignore some failures and accept empty responses.
An acknowledgment alone consequently cannot justify freeing Linux DMA memory.
Those commands also require a working HFRP channel; they are not an established
repair for the earlier failed transport handshake.

## What is implemented and what remains unproven

`make check` exercises real C protocol and driver functions with modeled kernel
edges. Startup tests inject failures at every HFRP preparation gate and verify
balanced cleanup before publication, retained PM/module/context ownership after
publication, preservation of the initial handshake, and rejection of retries.
Shutdown, binding, queue and DSP-client tests cover additional lifetime and
concurrency failures; they do not model an entire SoC or firmware controller.
Seeded ASan/UBSan property tests add 32,768 malformed HFRP replies and 16,384
accelerator IOCTL/lifetime combinations. They exercise actual repository C
functions with bounded host buffers; they do not execute DLA task firmware,
model arbitrary invalid userspace pointers or prove concurrent hardware safety.

The remaining hardware contracts are:

1. Establish the Surface's missing HFRP startup prerequisite by tracing the
   earlier vendor initialization and comparing actual firmware resources and
   provider state. No missing ACPI power method, GPU dependency or BIOS defect
   has been established.
2. Establish reset/quiesce semantics independently of command acknowledgment
   before implementing recovery from an uncertain firmware/DMA state. Until
   then, failed channels retain resources under the
   [shutdown contract](shutdown-reload.md).
3. Define a resource/provider contract for the fixed SoC display-VBIOS locator
   and the shared SROOT service. Sibling matching currently rejects ambiguity;
   no firmware association is fabricated to replace missing `_DEP`/`_DSD` data.
4. Validate hostile task isolation before treating the accelerator as a sandbox.
   Existing access policy is a trusted-client boundary. VFS/udev permissions
   control access; `require_rawio=1` additionally requires `CAP_SYS_RAWIO`.
5. Broaden native qualification across boards, fully powerless starts, repeated
   physical sleep and failures during active work. Offline results do not
   establish those properties.

The companion P16 kernel36
[native qualification](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux/blob/main/docs/evidence/kernel36-native-20261117.json)
records numerical NPU checks, audio reloads and physical s2idle with the
previously deployed driver. It does not qualify this diagnostic revision or
the Surface. Building this revision does not install it, change the running
module or require a reboot.
