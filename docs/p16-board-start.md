# P16 BIOS-300 board policy and HFRP startup

The actual P16 policy is now available. The original NVIDIA 617.42 Windows
parser and complete policy initializer both return zero on the physical ROM
capture. All fields used by the native parser and the derived gating policy
match the original code. This resolves the missing board-policy input; it
alone does not establish a successful NPU firmware boot. The physical sequence
has now reached successful firmware startup, bypass-task completion and one
numerically verified FP16 ReLU execution; see
[the physical result](#physical-numerical-relu-success).

## Reproducible read-only acquisition

Windows RVA `0xd3d2c0` reads physical locator `0x1182bc80` and maps
`((locator << 12) + 0x80000600) & 0xffffffff` for exactly `0x110000` bytes.
On this P16 H7607BA, BIOS H7607BA.300, kernel
`7.2.8-23-omarchy-p16-asus-npu2`, the locator is `0x39600`, selecting
`0xb9600600`. The whole window is inside reserved `b9600000-b97fffff`.

`scripts/read_p16_vbios_locator.c` restricts itself to that exact machine,
kernel, locator address and observed reserved window. The default operation
only reads the locator. `--capture` reads the bounded window twice, compares
the copies, and creates a new mode-0600 file. It neither writes registers nor
binds a driver. No arbitrary address/size option exists.

```sh
cc -O2 -Wall -Wextra -Werror scripts/read_p16_vbios_locator.c \
  -o results/read_p16_vbios_locator
pkexec "$PWD/results/read_p16_vbios_locator" \
  --capture "$PWD/results/p16-bios300-physical.rom"
venv/bin/python scripts/verify_p16_board_policy.py \
  results/p16-bios300-physical.rom > results/p16-board-policy-verified.json
```

Do not repeat acquisition over an existing file. The captured vendor bytes
stay ignored, not redistributed. The full-window SHA256 is
`a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c`
(1,114,112 bytes). The preceding aligned PCI image begins at window offset
`0x27f00`; the BIT header is at `0x280b0` and its ASCII letters at `0x280b2`.
The window need not begin with `55aa`. The directory is at `0x283b0`;
frequency, tuning and gating tables are at `0x5272b`, `0x5273d`, `0x5275d`.
PCIR identifies `10de:2e03`, matching the installed GPU. The image contains
`GB20B T254 Production VBIOS` and `Version 9a.0B.31.00.04`, also matching the
live driver's VBIOS version.

| Parsed input | Actual value |
| --- | --- |
| Operating / entry / exit frequency | 430,000 kHz each |
| Power-cap word / nine tuning words / gating word | All zero in the actual table |
| Frequency flags / gating flag | `[1,1]` / `1` |
| Gating bytes | `[1,1,3]` |
| Feature mask / general word | `0` / `512` |
| General header / allocation words | `[32,14,305]` / `[16,8,0,0]` |
| Derived request mask | `0x777` |
| Derived policy bytes | `[0,1,1,0,1,3,0,1,15]` |
| SLCG override / HAL flags 47d,47e,484,485 | `0` / `[1,1,0,0]` |

Zero tuning values are measured board inputs, not invented fallback values.
The verification harness executes original parsing and initialization in
Unicorn; its OS/setup callbacks remain stubs. It does not simulate power rails.

## Other acquisition paths and their limits

The optional `CONFIG_N1X_GPU_IOVA_OBSERVE=m` module observes existing GPU
IOMMU translations via the CPU page-table walker. It performs no device bind,
DMA allocation, MMIO/RPC or target-memory read. Nine scratch IOVA pages mapped
to noncontiguous CPU pages. `capture_gpu_iova_frts.py` copies only validated,
reserved RAM pages via `/proc/kcore`, with stable-map and double-copy checks.

The observed 36-KiB GFWI snapshot (SHA256
`b765fae8bfa807e12d3e61aeda198bdd82b04d2c35ab39098cd4067bf0e79256`)
has an offset-2048, length-34816 header but **no BIT/PCIR image** in its stated
payload. A CPU-contiguous extent hypothesis also lacked BIT. Neither is used
as board policy. The evidence does not distinguish overwritten allocation
from stale scratch metadata.

The GSP's original private `0x20800801` handler uses a 16-byte size/pointer
structure and copies a cached ROM. Its zero-size/null-pointer path is replayed
without patching that handler. A physical zero-size probe returned
`NV_ERR_NOT_SUPPORTED` through the Linux host API. No guessed pointer-bearing
request was sent. See `test_gsp_rom_control.py` and the explicit
`--probe-rom-api` option in `read_nvidia_vbios_dump.cpp`.

## Linux prelude

The manual bootstrap now targets the running `npu2` kernel. It maps only the
first 512 bytes of the separately audited HFRP resource `0x31380000`, with
distinct read/write offset allowlists. The DLA/SROOT DMA-domain and machine
guards still apply. Both endpoints' ACPI methods/resources were audited in
[acpi-audit.md](acpi-audit.md).

With explicit `enable_hfrp=1`, the root-only `prepare_hfrp` trigger:

1. Loads `nvidia/n1x/p16-bios300-physical.rom`, checks full size and SHA256,
   parses it and derives gating.
2. Takes runtime-PM ownership and requires observed HFRP stamp `0x20251722`.
3. Pins the module before the first write, performs the bounded handshake,
   discovers the version, sends board-policy startup configuration, then
   sends power request 7.
4. Publishes the completed stage, command, reply bytes and error in
   root-readable `hfrp_status`. Only successful completion enables the
   separate SROOT upload trigger.

No IRQ handler or automatic upload is installed. Unknown asynchronous replies
fail the channel. After the first write attempt, all outcomes retain the
module/runtime references until reboot; no second handshake or reset occurs.
Suspend is rejected while that state is retained. The subsequent host-only
buffer allocations do not publish descriptors until their existing explicit
commands; the Windows additional-setup callback's allocations were separately
replayed in [region5.md](region5.md).

```sh
venv/bin/python scripts/test_linux_hfrp_prepare.py
venv/bin/python scripts/test_linux_bootstrap.py
venv/bin/python scripts/test_linux_bootstrap_guards.py
venv/bin/python scripts/test_hfrp_setup.py
make -C /lib/modules/7.2.8-23-omarchy-p16-asus-npu2/build \
  M="$PWD/driver" n1x_npu_bootstrap.ko
```

The lifecycle test compiles the actual Linux prelude and native ROM parser,
checks the genuine capture/hash, injects each phase failure, and verifies no
later phase or repeat attempt occurs. OS/transport edges are synthetic there;
packet/peer fault tests remain separate. The physical prelude now satisfies
those response contracts on this board; IRQ roles remain unqualified, and
no accel API or inference readiness is claimed.

The repeatable qualification entry point is:

```sh
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through prepare_hfrp --log "$PWD/results/NEW-hfrp-run.jsonl"
```

It requires initially unbound devices, checks the machine/module/capture,
selects only the dedicated DLA/SROOT DMA domains, installs the hash-checked
local ROM if absent, and loads the manual module. Each phase is preceded by
a durable log entry; one unbuffered sysfs write follows, then recorded state
and the phase's success predicate are checked. Failure stops the sequence.
For a separately reviewed longer prefix, `--through` accepts the existing
phase names up to `bypass_task`. Even bypass success is not numerical inference
or production-driver readiness. No module autoload, boot-menu change or reboot
is performed. Do not rerun after an attempted phase in the same boot.

A successful HFRP-only run can be continued explicitly, without loading a
second module or repeating the handshake:

```sh
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --after-hfrp --through bypass_task --log "$PWD/results/NEW-after-hfrp.jsonl"
```

This requires the loaded module's source version to match the reviewed build,
HFRP phase 5/configured/power-accepted with error zero, and **no earlier upload
attempt**. A failed or completed upload is never resumed/retried through it.
The offline runner tests cover both prefixes and stop on rejected writes or
missing success evidence.

## Physical HFRP result, 2026-10-27

At 20:42:47 +01:00 on `7.2.8-23-omarchy-p16-asus-npu2`, boot ID
`6c8ae50e-4de7-4b42-b5ca-24b896556b8b`, the manual prelude completed with error
zero. The module SHA256 was
`51a8a948ab5df17aca4923d8f42641b079351d32daf1ef54bf1412d2b8403b2a`.
The machine remained in `performance`; only dedicated groups 19/24 were moved
from identity to DMA. Both guarded platform endpoints bound successfully.

Observed result: handshake succeeded; stamp `0x20251722` selected version 1.5;
all eight setup commands passed their transport, status and payload-shape
checks; command 400 returned header `0xa06`, two payload bytes `8c00`, status
zero. Final state was phase 5, READY, no pending sequence, configured and
power-accepted. The last statistics reply was `0x91e`, length 26. The durable
local record is `results/p16-hfrp-qualified-20261027.jsonl`.

This validates the recovered configuration/power protocol on this machine.
It is not a physical rail measurement or proof that the NPU firmware has
started. At this boundary no SROOT upload, DMA buffer, ping, region-5,
configuration import or task had been attempted. PM/module lifetime remains
held; no automatic reset/unload was performed. No new Xid, SError or IOMMU
fault appeared in the inspected kernel log.

## Physical firmware startup and bypass completion

At 20:45:40 +01:00 in the same boot, the explicit `--after-hfrp` continuation
completed every stage through `bypass_task`. Durable local record:
`results/p16-after-hfrp-20261027.jsonl`. The first successful module and both
execution logs are archived in `results/p16-first-success/`.

| Stage | Physical result |
| --- | --- |
| SROOT variant 0 | Reply `0x18`, status/detail zero, DLA idle observed |
| Upload backing | SROOT DMA `0xfffe0000`, retained |
| DMA-word ping | `0x44444444`, verified |
| Region 5 | Registered with a matching zero-status response |
| Config export / import | Replies `0x1602` / `0x1502`, accepted |
| Low-power config import | Reply `0x1702`, accepted with the then-existing provider-4 fallback |
| Bypass task | Reply `0x702`, data `0xfffdfd0`, accepted |
| Completion | SWGEN0 observed and fence `0x12345678`, verified |

This establishes firmware execution, bidirectional host DMA/control traffic,
configuration acceptance and one scheduler/postaction completion on the P16.
The descriptor uses BYPASS_EXEC, so no neural-network arithmetic is established.
No firmware authentication bypass, core-start register write or automatic retry
was used. The formerly rejected initial variant now succeeds after adding the
actual HFRP setup/power prelude. That strongly implicates the missing prelude;
it does not assign verified meanings to the earlier status/detail codes.

All published DMA buffers and runtime/module references remain held. No reset,
unload, suspend/resume or production recovery mechanism has been qualified.
No new Xid, SError or IOMMU fault appeared in the inspected kernel log.

## Board-correct low-power setting and next numerical test

The first successful run still used the old command-23 zero-input fallback 4,
which produced the raw word 8000. The captured policy instead has provider
`+0xf4 = 1`; the full original initializer preserves it. The current code uses
that value (raw word 2000) and the derived HAL flag, with the Windows fallback
only when the board value is zero. The actual Linux phase matches all 4096
bytes produced by the original Windows builder for provider 1; its raw HAL-1
set/get roundtrip and 30 lifecycle/failure cases pass. That earlier live instance was retained until reboot; the next boot accepted
this correction, as recorded below.

The new optional `relu_probe` phase requires the already verified bypass and
the same pinned board setup. It requests an exact 2,834,432-byte template,
SHA256 `b5792ecfbdc5d190fec9cdf7ecc17c7ba6919a42d5c1528e2acbcc2fc47f997c`.
The graph is one FP16 ReLU, `[1,32,7,7]`, with 3136-byte CHW16 input/output.
Its preparation is reproducible with `scripts/prepare_p16_relu.py`; generated
vendor/compiler bytes stay ignored. No arbitrary model, address, length or
command is accepted from userspace.

The kernel checks the full template hash before allocation, requires a
4-KiB-aligned coherent 36-bit DMA extent, constructs its own eight-entry address
list and fenced descriptor, and uses the same completion loop as the proven
bypass. The output starts poisoned. Only command acceptance, SWGEN0, fence,
unchanged input digest and expected output digest together pass the numerical
check. Expected output SHA256:
`6e043c4c2ae0ddfcaca4341ab36f15fdebf066157af5da6483bc56cd6786404f`.
The CPU reference was checked independently with NumPy and ONNX.

`test_linux_relu.py` checks the actual Linux preparation/lifetime/oracle code
in 27 cases, including transport success with unchanged output and corrupted
input. The exact prepared bytes also run through the unmodified raw HAL-1
reset/trap/DMA/scheduler code to engine wait, with the expected seven engine
address pairs. This emulates control processing, not engine arithmetic. The
shared completion loop retains its 44 fault/lifecycle tests. The complete
runner has 59 prefix/failure/precondition tests. Normal and UBSan checks pass.

The rebuilt module targets the existing `npu2` kernel; source version
`E936E960ECE1CFAEFA88E1D`, SHA256
`9a5b2b90e8477ab5ca9660f99a8c54e68009f02fe14b55e4bee2efec467f1e85`.
Its exact build/source/template manifest is
`results/npu2-relu-build-manifest.json`. This build was loaded in the next boot
and produced the idle-notification stop recorded below. The command for a
fresh boot into the same `npu2` entry remains:

```sh
powerprofilesctl set performance
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through relu_probe --log "$PWD/results/NEW-relu-start.jsonl"
```

No new kernel/UKI or boot-menu change is needed. General model submission,
an accel userspace API, asynchronous IRQ handling and safe teardown remain
outside this one-attempt qualification path.

## Idle notification blocked the next task, not firmware startup

Boot `5338c073-de5d-44a7-930e-4a22bacfdd20` used the above `E936...` module.
HFRP setup/power, variant-0 upload, ping, region 5, config import and the
board-correct provider-1 LPWR import all succeeded. Before bypass submission,
IRQSTAT was `0x81`; the old preflight returned `-EBUSY` without allocating or
publishing a task (`task_attempted=0`, `task_retained=0`). ReLU was never tried.
The durable record is `results/p16-relu-live-5338c073.jsonl`.

A bounded, read-only capture found mailbox `{word=0x0000000c,data=0}`, with
IRQSTAT `0x81` both before and after. `scripts/read_p16_dla_mailbox.c` maps only
physical page `0x19002000` read-only and reads logical registers 8, 0x40 and
0x44. It requires this exact machine, BIOS, kernel, old loaded module source
version and successful HFRP power status. Its version guard is intentionally
pinned to this diagnostic, not a general MMIO access interface. It performs no
ACK, reset or other register write. Capture: `results/p16-mailbox-5338c073.json`.
The old module, manifest and both logs are archived in
`results/p16-idle-blocked-5338c073/`.

Message 12 is `DLA_MSG_IDLE_TIMEOUT` in NVIDIA's local R38 NVDLA sources.
The original raw HAL-1 idle consumer at `0x107318` emits `{12,0}`. A new
regression executes that consumer and the original notification transport
without firmware function stubs: no ACK leaves it waiting, while the native
Linux capture/clear/ACK releases it, preserving unrelated IRQ bit 0. The
Windows IRQ handler at `0xd48ce0` also ACKs without an error report or decrement
of outstanding work unless SWGEN0 is present. Twelve cases cover policy flag
`+0x47d` 0/1, IRQ 0x80/0x81/0xc1 and outstanding counts 0/2. These tests keep
the host context ineligible for power-down. Windows separately gates a host
power-policy callback after ACK; no such transition is claimed or implemented.

The Linux task path now drains only exact `{12,0}` notifications before DMA
allocation and recognizes them during task polling. A notification does not
replace the command reply, count as acceptance, signal completion, or clear
SWGEN0. Unknown preflight frames remain untouched. Drain is bounded to 16
notifications; an uncertain ACK or exhausted drain blocks another attempt.
The existing task deadline, acceptance/fence/SWGEN0 checks, error priority and
DMA retention remain intact. `status` and `relu_status` expose idle counters
and blocked state. The qualification runtime reference keeps the NPU powered;
idle notification handling is not suspend/resume support.

Validation: 65 native bypass lifecycle cases, 14 baseline I/O failure
positions plus idle-preflight failure positions, 27 ReLU preparation/lifetime
cases, the original-firmware transport checks, 396 idle-consumer cases and
Windows response replay all pass. Native lifecycle tests also pass UBSan.
`make test-p16-start` includes the response and idle regressions. The module
build succeeds against the existing `npu2` headers.

The new module has source version `71627FA86AE8901BD522655`, SHA256
`96700ef0a1db775ad6be875f9e9be0a3b8023c68e0a885764b11dcaa9af322fa`;
manifest `results/npu2-idle-relu-build-manifest.json`. It is built but cannot
replace that pinned live module. Physical verification required a fresh boot,
which subsequently passed as recorded below. For reproduction, use the same
`npu2` entry and the `--through relu_probe` command above with a new log filename. No kernel, UKI or boot-menu change is required.


## Physical numerical ReLU success

On **2026-10-27 at 21:25:52 +01:00**, boot
`0dd26c89-bbd0-4bbc-b9ce-99cacfa37675`, the complete nine-phase qualification
through `relu_probe` passed on `7.2.8-23-omarchy-p16-asus-npu2`. The system was
set to `performance` before loading the bootstrap. Source commit `32cb2bd`,
module source version `71627FA86AE8901BD522655`, module SHA256
`96700ef0a1db775ad6be875f9e9be0a3b8023c68e0a885764b11dcaa9af322fa`.

HFRP protocol 1.5, all eight setup commands, power request, variant-0 upload,
ping, region 5, config export/import and provider-1 LPWR import succeeded.
Both the bypass and ReLU paths handled one pending idle notification, with
`idle_blocked=0`, before successful completion.

| Evidence | Bypass | FP16 ReLU |
| --- | --- | --- |
| Error | 0 | 0 |
| Reply / data | `0x702` / `0xfffdfd0` | `0x702` / `0xfff8000` |
| Acceptance / SWGEN0 | Both observed | Both observed |
| Fence | `0x12345678` | `0x12345678` |
| Completion verified | Yes | Yes |
| Numerical match | Not applicable | `numeric_verified=1` |

The ReLU graph has shape `[1,32,7,7]`, FP16 CHW16 storage, 1568 elements and
3136 bytes in each input/output. The output started poisoned (`0xa5`); the
Linux driver does not compute ReLU or copy a reference tensor into that output.
It submits the compiler-produced graph without BYPASS_EXEC, waits for
acceptance, SWGEN0 and the fence, then hashes both coherent DMA buffers.

- Unchanged input SHA256:
  `73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45`.
- Complete output SHA256, matching the independently checked CPU reference:
  `6e043c4c2ae0ddfcaca4341ab36f15fdebf066157af5da6483bc56cd6786404f`.

The durable phase log is `results/p16-relu-live-0dd26c89.jsonl`. The full kernel
log is `results/p16-relu-kernel-0dd26c89.log`; it independently records
`ReLU error=0 accepted=1 completed=1 numerical_match=1` and both hashes.
No Xid, SError, IOMMU fault, Oops or lockup appeared from module load through
the captured post-test log. The module, original build manifest, phase log,
kernel log and a hash-indexed summary are preserved in
`results/p16-first-numerical-success/`. These local generated artifacts remain
ignored; this source document records the reproducible result.

This establishes a complete manual Linux startup and one real NPU arithmetic
execution on this board. It does not establish arbitrary-model compatibility,
registered interrupt handling, repeated execution, sustained performance,
suspend/resume, safe reset/teardown or a general `/dev/accel` interface. The
bootstrap deliberately retains published DMA and runtime references until
reboot; nothing was added to automatic boot loading.
