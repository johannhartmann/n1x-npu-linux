# HFRP and firmware IRQ evidence

The auxiliary P16 aperture has a verified Windows HFRP register/interrupt
protocol correspondence with the pinned L4T source. **The physical P16 GSI
assignment is still unverified.** No Linux IRQ is registered by this work, and
the bootstrap still does not map/write the auxiliary aperture. HFRP response
rings are now implemented as separate bounded callback helpers, described
below. Clock/power transitions and recovery are not implemented by the IRQ
capture helper.

## Inputs and boundaries

The Windows input is the existing SHA-pinned NVIDIA 617.42 `nvlddmkm.sys`.
The audited P16 DLA resources are `0x19000000/0x40000` and
`0x31380000/0x10000`, followed by level/high GSIs 463 and 800 in ACPI resource
order. Windows's recovered resource provider names the second MMIO entry
**resource id 2**, not Linux aperture index 1. Do not equate those identifiers.

The local primary GPL reference is under
`sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/`:

| Reference file | SHA-256 |
|---|---|
| `port/pm/nvdla_pm_hfrp_reg.h` | `ca7bc4d365772cedcc09ae08b43cd2f232a581b14c748d9b8642e33b2e3f6e89` |
| `port/pm/nvdla_pm_hfrp.c` | `c518045a2afa20890015cd02f400b2f35d1afd73ba2d926cbfbd41dbae1bc0b8` |
| `port/fw/nvdla_fw_riscv.c` | `86a6e30855388b53636553f6e0128cde6c4c8b18d0bc86a247aa90f16e1c2a05` |

In that source, `nvdla_fw_init` requests IRQ index 0 for `s_riscv_isr`.
`nvdla_pm_init` uses `NVDLA_HFRP_APERTURE_IDX=1` for both the HFRP aperture and
IRQ index, registering `s_hfrp_isr`. This makes GSI 463=firmware and 800=HFRP a
**candidate based on the P16's resource order**, not a measured identity or a
reason to enable either interrupt blindly.

## Original Windows initializer and MMIO chain

Windows initializer RVA `0xd4aeb8`, with its observed constructor argument 0
from `0xd3d988`, sets these class fields. The test compiles the actual pinned
L4T header to obtain independent reference offsets; all eight agree:

| Windows class offset | Register offset | L4T reference role |
|---|---|---|
| `0x20` | `0x110` | client buffer offsets |
| `0x28` | `0x114` | command buffer start |
| `0x30` | `0x188` | server buffer offsets |
| `0x38` | `0x18c` | response buffer start |
| `0x40` | `0x100` | IRQ_IN_SET |
| `0x44` | `0x104` | IRQ_OUT_SET |
| `0x48` | `0x108` | IRQ_IN_CLR |
| `0x4c` | `0x10c` | IRQ_OUT_CLR |

This is a register-layout correspondence, not proof of every queue field or
live HFRP firmware version. The initializer has another argument-dependent
layout; this comparison concerns the observed mode-0 constructor path only.

The original callback chain is also executed, not replaced with a generic
read/write assumption. Class `+8` points at `0xd3d880`, `+0x10` at `0xd3d8d0`,
and `+0x18` points to the provider link. The read wrapper follows that link and
calls `0xd39878(owner, 2, offset, out)`; the write wrapper tail-calls
`0xd39f38(owner, 2, offset, value)`. Thus the HFRP accesses are tied to Windows
resource id 2 rather than to the DLA remapper or SROOT mailbox.

## Original IRQ capture and parent dispatch

The original HFRP capture function `0xd4b308`:

1. Reads IRQ_OUT_SET through class field `+0x44`.
2. Increments a 32-bit counter at class `+0x40e0` when status bit 1 is set,
   including wraparound from UINT32_MAX to zero.
3. Writes the entire observed status to IRQ_OUT_CLR through field `+0x4c`.
4. ORs the raw status into class `+0x40dc` and returns whether it was nonzero.

It even writes zero for a zero status. Its outer wrapper `0xd3dc28` gates access
on the provider's enable field, and records timestamps for bits 26/28/30 and
27/29/31. These agree with the L4T start/end notifications for entering clock, power
and rail gating. An end bit is **not** an exit/wakeup indication. No timestamp
unit is inferred. The wrapper inspects accumulated pending bits: when bit 26
is already pending and bit 27 arrives, the test observes both timestamp slots
rewritten. Do not copy that as a validated latency measurement. The test does
not exercise physical transitions or infer safe power actions.

The parent function `0xd39198` checks HFRP through parent `+0x300` first. If the
enabled HFRP callback reports nonzero status, this invocation skips the primary
DLA probe. Otherwise it calls the primary object's status callback `+0x280`;
a nonzero successful result is accumulated at `+0x4a0` and its `+0xe0` callback
is invoked. The concrete HAL-1 callback clears masked IRQ bits, as established
below; deferred work is queued separately by the entry wrapper. A primary
accessor error prevents accumulation/clear dispatch. The original 48-case
parent test uses explicit primary status/callback
stubs. A further 60 cases now execute the original primary initializer and
callbacks described below; only lower OS/MMIO/provider boundaries are modeled.

This proves a shared software dispatch path and HFRP-first priority, not which
physical wire invoked it. In particular, simultaneous modeled pending sources
are not both consumed in the same parent call. The eventual Linux IRQ design
must account for this and preserve deferred events instead of dropping them.

A static import-reference scan also found the generic adapter's
`IoConnectInterruptEx` call at RVA `0x1ad1304`, constructing a message-service
callback plus line fallback (`0xe3c5e0` / `0xe3c5f0`). It has **not** been tied to
the NPU PDO or its ACPI IRQ descriptors. Microsoft's API distinguishes
PDO-derived line registration and message registration with fallback; an import
or fallback message index cannot identify the P16's GSI order.
See [IO_CONNECT_INTERRUPT_PARAMETERS](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_io_connect_interrupt_parameters)
and [line-based registration](https://learn.microsoft.com/en-us/windows-hardware/drivers/kernel/using-the-connect-line-based-version-of-ioconnectinterruptex).

## Minimal C capture component

`driver/n1x_hfrp.c` implements only the status read and raw W1C acknowledgment
against HFRP-relative I/O callbacks. It is not linked into the observation or
bootstrap module, and has no binding, IRQ registration, power operation or
response-ring consumer.

Invalid callback/output arguments cause no I/O and leave the output unchanged.
Afterward result fields preserve read validity, raw status, ACK attempt and ACK
success. A zero status returns EAGAIN without Windows's no-op zero write.
Read/write errors propagate. In particular, an ACK error leaves the captured
status available for recovery, rather than falsely reporting success.

Those differences are deliberate: the original read wrapper ignores its lower
accessor's error and the original capture routine ignores its writer's return.
Synthetic failure controls reproduce both behaviors. Linux must not copy them.
The returned raw status preserves unknown bits and does not mean higher-level
HFRP work has completed. Caller-owned queuing/counter accounting, recovery after
uncertain acknowledgment and DMA lifetime remain responsibilities of a future
IRQ implementation.

## Reproduction and remaining work

```sh
venv/bin/python scripts/test_hfrp_irq.py
make kernel-check
```

The test executes the original initializer, MMIO wrappers, HFRP capture and
parent dispatcher with synthetic OS/MMIO boundaries. It checks eight exact
L4T header offsets, 72 C capture/fault comparisons, 48 parent-dispatch cases with timestamp checks,
Windows swallowed-error controls, primary-error suppression and four C
preflight rejections. Counter/pending state and all original nonzero success
MMIO traces are checked. The C component compiles against current and installed
test-kernel headers; the isolated object build/manifest is in
`results/npu1-hfrp-build/`.

No interrupt was requested, device register read/written, bootstrap loaded or
firmware started by these tests. Hardware GSI routing, HFRP queue processing,
power-state coordination, full initialization and inference remain unverified.
Do not map the auxiliary block, register an IRQ or add power writes solely from
the index analogy. The next evidence still needs to connect the generic Windows registration
context to this NPU wrapper and its translated resources, or capture actual
source status during an explicitly qualified hardware experiment.


## NPU entry wrapper and original primary callbacks

A static executable-section branch scan found the direct call
`0xd36fa4 -> 0xd39198`. Its containing entry wrapper is **0xd36ee8**; the
additional static caller `0xdd21e0 -> 0xd36ee8` is independently checked by
instruction decoding in the test. The latter wrapper at 0xdd2188 includes
logging around the call; it does not itself identify a Windows PDO or GSI.
This is a concrete NPU software path, still separate from the generic
IoConnectInterruptEx/vtable path above.

Executing `0xd36ee8` shows that it obtains the owner from global `0x1512148`,
looks up the parent through `0x93630`, gets an outer object through `0xd39160`,
then calls the shared HFRP-first/DLA dispatcher. It finally invokes
`0xd3b4a0(outer, parent->0x160, 0)` and returns 1. The original wrapper tests
modeled that helper; the additional DPC tests below execute it through
`KeInsertQueueDpc`. The tested input register x0 does not affect this path.
Its external semantic name is unknown; the test values are not IRQ numbers.

Thirty cases vary that input over 0, 1, 2, 0x12345678, UINT32_MAX, HFRP status
over 0/2 and primary status over 0/0x40/0xc0. With modeled lookups succeeding,
the wrapper returns 1 and reaches the final helper even when neither source
is pending. A zero-global control returns 0 before any dispatch or final
helper. Linux must not copy the unconditional successful return as evidence
that a shared IRQ belongs to this device or a task completed.

The original HAL-1 initializer `0x1951238` sets primary context `+0x280` to
**0xd48830** and `+0xe0` to **0xd45670**. The extended test executes that
initializer, verifies both installed pointers and then executes both original
callbacks through the parent dispatcher, replacing the previous primary stubs
for these additional cases:

- `0xd48830` tail-calls the generic accessor with **resource id 0**, logical
  offset **8**, preserving its error return. This reads DLA IRQSTAT, distinct
  from HFRP resource id 2 / offset 0x104.
- On a successful nonzero status the parent ORs all raw bits into primary
  `+0x4a0`. A failed accessor neither accumulates nor dispatches a clear.
- `0xd45670` follows primary `+0x68` to the provider, reads its ownership mask
  at `+0x54`, ANDs it with accumulated `+0x4a0`, then calls provider vtable
  **+0xb0** with a pointer to the filtered u32. The provider callback is an
  explicit boundary in these original 60 cases. The concrete callback is now
  identified as IRQ clearing below, not a scheduler-completion notification.

Sixty combinations vary HFRP enabled/status, primary IRQSTAT (including an
unknown bit 0x100) and ownership masks 0/0x40/0xc0. They verify HFRP-first
suppression, raw pending accumulation and filtered provider payloads. A
nonzero raw status with ownership mask zero still calls the provider with
zero; no unobserved filtering policy is inferred. The separate accessor-error
control confirms no accumulation/provider call.

The earlier 72 C capture/fault and 48 parent/timestamp cases still pass. These
new cases perform no MMIO on this machine and change no Linux IRQ registration.
They recover more of the required software dispatch/ownership chain; physical
463/800 assignment, GIC delivery, deferred-work consumption, reset/recovery and
numerical execution remain unresolved.

## Concrete DPC path and primary interrupt acknowledgment (2026-10-27)

The additional 35 cases in `test_hfrp_irq.py` execute the original DPC allocator
wrapper, IRQ entry, primary clear callback, DPC callback, pending-bit consumer
and HAL-1 SWGEN0 handler. They close the earlier provider/notification stubs for
this selected path. Windows allocation, `KeInitializeDpc`, `KeInsertQueueDpc`,
locks, lookups, timestamps and MMIO remain modeled. HFRP is absent and SWGEN1
is excluded in these additional cases; physical IRQ routing is not tested.

The PE import table identifies `0xe3e108` as `KeInitializeDpc` and `0xe3e110`
as `KeInsertQueueDpc`. Constructor code at `0xd39694..0xd396a8` passes callback
`0xd39380`, parent as its context, and parent `+0x160` as output storage to
helper `0xd3b428`. That helper allocates 64 bytes and initializes the DPC.
IRQ wrapper helper `0xd3b4a0` queues that object with both system arguments
zero. Its input `w2` is used for tracing, not forwarded as a DPC argument.

The concrete primary factory `0x195d038` sets main vtable `0x108e9a0`, the
subobject vtable at primary `+8` to `0x108e9b8`, and primary `+0x68` to that
subobject. These constructor stores were inspected statically; tests construct
the corresponding objects and execute the original HAL initializer.

| Step | Original code / dispatch |
| --- | --- |
| Read and accumulate IRQSTAT | `0xd39198` -> `0xd48830`; pending bits at primary `+0x4a0` |
| Filter ownership mask | primary `+0xe0` -> `0xd45670` |
| Acknowledge hardware IRQ bits | subobject vtable `+0xb0` -> `0x641b90` -> primary `+0x290` -> `0xd4a8f0` |
| Queue deferred work | `0xd3b4a0` -> `KeInsertQueueDpc` |
| DPC entry | `0xd39380`, parent context; optional HFRP work first |
| Dispatch primary deferred work | parent `+0x178` -> subobject vtable `+0x218` -> `0x142470` |
| Consume pending snapshot | main vtable `+0x298` -> `0xd43fd0` |
| Process HAL interrupt | primary `+0x110` -> `0xd48ce0` |

`0xd4a8f0` writes the filtered DWORD to resource 0, logical offset 4
(IRQCLR). Thus the previously labeled "schedule" callback is the hardware
acknowledgment path. The pending snapshot remains the **raw** accumulated
status, including bits outside the ownership mask; the DPC is not passed the
filtered clear mask. With ownership zero, a nonzero status still produces a
zero IRQCLR write followed by raw-status processing. This observation does
not establish ownership of unknown interrupt bits on Linux.

The DPC consumer skips work when primary `+0x524` is nonzero or there are no
pending bits. Otherwise it atomically XORs the captured snapshot out of the
pending word before invoking the HAL callback. The test is single-threaded;
it does not prove race-free behavior for a newly arriving identical IRQ bit.

If the HAL callback cannot acquire its lock, it ORs the snapshot back into
pending state and queues the same DPC again. The test denies the first lock,
allows the second, and verifies two DPC executions with only one counter
decrement. A modeled already-queued DPC returns false from insertion, but the
original IRQ wrapper still returns 1 and the queued consumer processes the
pending snapshot. That boolean is not an inference-success result.

The 32 matrix cases vary raw bits 0/0x40/0x100/0x140, ownership masks 0/0x40,
counter values 0/2 and whether a DPC was already queued. The counter is
unchanged before DPC processing. SWGEN0 then decrements a positive counter
once; an unknown bit alone does not. Three extra cases cover lock retry,
ignored IRQCLR write error, and disabled primary processing. The ignored
write error is vendor behavior, not a recommended Linux error policy.

No task ID, WDDM command completion or scheduler-notify call appears in the
executed SWGEN0 path. It reaches the already-known HAL counter handling and
returns. This narrows the search: the WDDM completion contract is not supplied
by merely queuing or draining this DPC. Preserve the Linux separation between
IRQ acknowledgment, output readiness and proven buffer retirement.

### Command-submission path

Static inspection finds a separate task path: KMD method-table record
`0x108e7c0` maps method `0x35800001`, input size 16, to `0xd45050`. That
handler reads two input QWORDs and invokes primary `+0x140` with flags 4.
The HAL-1 initializer installs `0xd4a400` in that slot; this function shifts
the descriptor address by 8 and invokes method sender `+0xf8` with command 7.
The [KMD task replay](windows-kmd-task.md) now executes the higher-level caller
`0xdd6888`, envelope builder `0xdd22c8`, command handler and HAL direct-send
branch, with generic object lookup and the low-level sender modeled. It also
feeds the original Windows postaction producer's output to the original
firmware consumer. Neither experiment establishes safe DMA reclamation.

Correction: the earlier lead at `0x19d8800..0x19d8808` selects an **object
handle** `0x35800001`; its actual command is `0x3580000a`, not task submit.

## Pre-loader reset and idle commands

Ghidra decompilation and radare2 ARM64 disassembly identify another required
area to resolve before extending the physical bootstrap: a parent of common
loader `0xd439c0` performs HFRP operations first. All addresses below are RVAs
in the pinned 617.42 KMD. The complete entry is **`0xd41740`**; `0xd4176c` is
an interior body block after its stack-cookie prelude and argument guard.
PE exception-table entries are not sufficient evidence of callable entry points.

`scripts/test_loader_prerequisites.py` executes the original complete parent
and its original helper `0xd3f120`, including the original stack-cookie helpers.
With a nonnull chain `outer+0x300 -> object+0x60 -> provider+0x18` and value
1 at the final object's +8, the observed order is:

1. Helper `0xd3f120(outer, mode, 1)` sends HFRP command `0x19b`.
2. The parent sends `0x19a` with the single byte `01`.
3. The parent sends another `0x19b` with a mode-dependent byte.
4. The parent calls the common loader with `outer+0x170` as its HAL object.

| Low byte of mode argument | First `0x19b` byte | `0x19a` byte | Final `0x19b` byte |
| --- | --- | --- | --- |
| 0 | `03` | `01` | `0c` |
| 1 | `00` | `01` | `04` |
| 2 | `02` | `01` | `08` |
| 3 | `01` | `01` | `00` |
| Other (tested 4) | `00` | `01` | `00` |

The mode's low byte is used: input 256 reproduces mode 0. The engine-reset
helper now establishes one caller using mode 0; see the
[reset/reload trace](windows-kmd-task.md#engine-reset-rm-command-and-firmware-reload-2026-10-28).
The meanings of the other caller modes remain unresolved.
Missing links or a capability value other
than 1 skip all three commands but still reach the common loader. A nonzero
second entry argument returns `0x56` before this sequence. Pending work at
outer+0x4a8 invokes callback +0xe0 first; its failure prevents the sequence.
Injected HFRP transport errors and firmware response errors are logged/ignored
by this parent, whereas the common loader's error becomes its result.

The NVIDIA-authored L4T R38.4 reference
[`nvdla_pm_hfrp_cmd_v1_3.c`](https://github.com/OE4T/linux-nv-oot/blob/33227347c89543042e0eda352db5de852983c8a3/drivers/video/tegra/host/nvdla/port/pm/nvdla_pm_hfrp_cmd_v1_3.c)
names command 410 (`0x19a`) **RESET** and 411 (`0x19b`) **RESET_IDLE**, both
with one-byte payloads. Its SHA-256 is
`22d224fc2ad2ccf0db5a7c0e2ae72f048a16dd5f49b261aa25bfad64075e8acf`;
the test verifies this hash and the four command/length definitions. This
establishes an ABI correspondence, not the physical meaning of each payload
bit or permission to reset the device. These HFRP commands are distinct from
the DLA firmware's SET/GET_LPWR_CONFIG commands.

The Windows send wrapper at `0xd3de30` takes its lock, forwards to `0xd4b568`
using object+0x60, releases the lock and records the response. The current
emulator stubs this wrapper, the common loader, memset, logging/timing and the
pending-work/CFG boundaries. **It does not execute HFRP transport, reset
hardware, authenticate firmware or show that this parent runs on a P16 cold
boot.** Seventeen cases cover modes, capability/pointer gates, entry rejection,
pending work and injected errors. The existing Linux bootstrap does not send
these commands; their necessity for the observed SROOT rejection is unproven.

A separate candidate is now excluded for the recovered selector-1 table:
the original initializer `0x1951238` assigns HAL +0x3d8 to `0x5be0`, whose
entire implementation is `mov w0, 0; ret`. Parent `0xd40060` calls this slot
with argument 1 before the common loader. The test executes both initializer
and target. Parent `0xd47700` instead calls outer +0xc8 with `(outer, 0, 7)`;
the outer callback is decoded below. Actual parent selection still needs
tracing. Do not equate outer +0xc8 with a primary HAL slot.

Reproduce offline:

```sh
venv/bin/python scripts/test_loader_prerequisites.py
venv/bin/python scripts/emulate_sroot_resources.py
```

`scripts/ghidra/ExportFunctions.java` exports selected functions with the input
SHA-256 and references. Use an installed Ghidra `support/analyzeHeadless` with
`-scriptPath "$PWD/scripts/ghidra" -postScript ExportFunctions.java OUTPUT ADDRESS...`.
For the large KMD, targeted `-noanalysis` imports are sufficient when entry
addresses are already verified; use `140d41740`, `140d3f120`, `140d3de30`
and `140d47700`. Export paths must be new. Shared stack-cookie helpers can
confuse inferred prototypes, so check assembly and original-code execution.
Projects, exports and vendor code stay in ignored `results/`, outside Git.

## Outer power callback and protocol-dependent encoding

The original device initializer `0x194fb08`, executed with profile 3 (the
existing REV_0034 classifier/profile chain), assigns **outer +0xc8** to
`0xd405f0`. Profiles 4 and 6 also assign that address in a separate exploratory
initializer run. The DLA selector-1 initializer instead assigns **HAL +0xc8**
to `0xd47700` and **HAL +0x210** to `0xd48b30`. These are different objects.

`0xd47700` calls outer +0xc8 with `(outer, 0, 7)` and only invokes the common
loader on success. `0xd48b30` takes the alternate path through `0xd41740`: it
passes byte 1 of its third argument as the reset mode, bracketed by provider
mask operations on bit `0x80`. This identifies how that reset mode reaches the
previously decoded sequence; it does not prove that either entry is the P16
cold-start callback. Reset should not be prepended to every upload based on
the reset-capable entry alone.

`scripts/test_hfrp_power_policy.py` executes the original profile-3 initializer
and **complete power callback `0xd405f0`**. HFRP send functions `0xd3dd40` and
`0xd3de30`, OS/timing/logging helpers, and notification function `0xd39ef0`
are recording stubs. The test supplies capability and version fields; it does
not run power hardware or transport. There are 48 cases.

The callback's primary command is **400 (`0x190`, POWER_CONTROL)**. Its request
mask is policy input, not a directly transferable wire value. With enable mask
`0x777` and no existing pending-state bits, the original code produces:

| Windows request | Effective operation correspondence | HFRP action bits |
| --- | --- | --- |
| `0x001` | clock on | `0x080` |
| `0x002` | power domain on | `0x004` |
| `0x004` | rail on, also power domain on | `0x00c` |
| `0x007` | clock, power domain and rail on | `0x08c` |
| `0x010` | clock off | `0x040` |
| `0x020` | power domain off | `0x001` |
| `0x040` | rail off, also power domain off | `0x003` |
| `0x100` | delayed clock off | `0x100` |
| `0x200` | delayed power-domain off | `0x010` |
| `0x400` | delayed rail off, also delayed power-domain off | `0x030` |

The names come from the NVIDIA-authored v1.3 implementation, not from guessed
meanings of Windows masks. The test compiles that original function, with only
transport/event stubs, and compares all ten v1.3 payloads byte-for-byte.
Windows and L4T agree on the nine action bits and third-byte PPS layout.
The test's PPS values are synthetic, not recommended settings or measured
frequencies. Windows chooses PPS using its current value and three policy
bytes at outer +0x31f/+0x322/+0x325; physical units remain unestablished here.

For link version **1.3**, Windows sends three bytes: little-endian action bits
followed by PPS. For major 1 and minor **at least 5** (tested 5 and 6), it sends
**two bytes**, omitting PPS. When outer +0x14d is set, delayed-action flags
also populate additional bits 9..14. Major 2 and versions 1.2 and 1.4 are
rejected with `0x40`. With version exactly 1.5, outer +0x14c can additionally
enable command `0x1a1` with byte `01` after clock-on; the L4T command table names
this SLCG_OVERRIDE. This flag does not establish that the P16 requires it.

The callback rejects a nonzero second argument (`0x56`) and conflicting
on/off request pairs (`0x3f`). Outer +0x4b0 gates permitted operations;
zero skips the power operation. Original initializer `0xd44710` normally
starts that mask at 7, adds optional gating capabilities from policy bytes,
and has a separate global disable branch. It has been disassembled, not yet
fully replayed together with its policy producers. Existing pending state
can suppress redundant operations. A transport error propagates; a nonzero
firmware status becomes `0xffff`. Those errors must prevent the loader when
this parent path is used.

## Power-wakeup prerequisite before engine reset (2026-10-28)

`scripts/inspect_kmd_reset_wakeup.py` resolves the previously modeled
outer `+0xe0` callback: original profile-3 initializer `0x194fb08` installs
**`0xd40060`**, together with power callback `+0xc8 -> 0xd405f0` and state
query `+0x108 -> 0xd41630`. It executes these functions from the original
reset parent `0xd41740`, which calls `+0xe0` when outer `+0x4a8` is nonzero.
That field participates in power-state bookkeeping; calling this only a
generic pending-work callback hid a firmware-wakeup prerequisite.

Twenty original-code cases use explicit outer profile 3 and primary HAL 1.
The primary constructor's synthetic selector initially installs HAL 2, so
the test explicitly reruns the original HAL initializer with selector 1;
these fixture values are not a claim about live Windows hardware discovery.
The atomic acquire/release routines execute, with a synthetic owner identity.
HFRP transport/replies, common loading, postinit method 23, IRQ setup and
OS timing remain recording boundaries. No physical reset is attempted.

With outer `+0x149 == 0`, the observed sequence is:

1. Acquire outer `+0x4ac` and mark primary `+0x5ac` busy.
2. Select wakeup operations from outer power bookkeeping/flags and invoke
   the original power callback. The baseline pending mask 1 with `+0x14b=0`
   emits v1.3 POWER_CONTROL `8c 00 00` (clock/domain/rail on, PPS fixture 0).
   With `+0x14b=1`, tested pending mask `0x10` emits `80 00 00`; mask `0x60`
   emits `0c 00 00`. These are synthetic policy cases, not recommended PPS.
3. After successful power control, invoke primary `+0x3b0 -> 0xd48890`.
   That original accessor reads **resource 0, offset `0x80`**. NVIDIA R38
   `nvdla_fw_riscv_reg.h` names this `riscv_os_version_r()` /
   `NV_PNVDLA_FALCON_OS`; it is **not IDLESTATE**. Header SHA-256:
   `385b24b30a4f53ae681974b0e66cb5c1d0d0a1b3cbbfec944f4bb2ccd2f0bd8a`.
4. A successful read of zero selects primary `+0x3d8` (HAL-1 no-op `0x5be0`)
   and the common loader. Any tested nonzero word (`1`, `0x12345678`,
   `0xffffffff`) skips this reload and uses postinit callback `+0x438`.
   A failed register read also takes that latter path: it is not fatal.
5. Perform IRQ setup, clear the busy flag, release the atomic owner, and
   return to the reset parent. On success it then sends RESET_IDLE/RESET/
   RESET_IDLE (`03`, `01`, `0c`) and calls the common loader again.

Thus **a zero OS-version word produces two loader calls**, one in wakeup and
one after the reset commands. Power-transport error `0x65` or firmware status
1 (mapped to `0xffff`) stops before the version read and reset sequence;
the original callback leaves its busy byte set but releases its owner.
Failure from the wakeup loader also prevents the reset sequence, although
the original callback still performs IRQ setup before returning that error.

With outer `+0x149 == 1`, the callback instead queries state through
`0xd41630`. The tested v1.5 path sends **`0x1af`**, zero request bytes, and
reads the low three bits of its one-byte response. Query bits 1 or 2 cause
the wakeup reload; bit 0 alone does not. No OS-version read occurs in this
branch. POWER_CONTROL uses the v1.5 two-byte encoding. A query transport
error, firmware error or empty response does **not** prevent the subsequent
power operation or reset in the tested pending-mask-1 fixture, and does not
trigger a fallback OS-version read. The empty response is treated like zero
because the original query initializes its byte and does not check the
returned length.

The added evidence identifies a power/firmware-presence prerequisite, not a
DMA drain. Linux must keep wakeup, firmware-presence, protocol completion and
physical retirement separate; it must not interpret OS_VERSION nonzero or
an ignored query failure as verified idle. This work does not enable a new
Linux reset path or relax retained DMA lifetime.

### Native power-state query

`n1x_hfrp_query_power_state()` in `driver/n1x_hfrp.c` now implements that
`0x1af` query over the existing bounded, sequence-matched channel. It requires
a valid discovered major-1/minor-5-or-newer protocol and an exclusively owned
READY channel. It sends no payload and accepts exactly one response byte.
`struct n1x_hfrp_power_state` exposes the full raw byte and a validity flag;
the original wakeup decoder only consumes bits 0..2. No additional physical
meaning is assigned to the other bits.

An accepted attempt invalidates any old snapshot before I/O. Transport and
firmware errors propagate with the existing exchange diagnostics. A complete
firmware-error response leaves transport usable but provides no valid state;
an empty or oversized response returns `-EBADMSG` and fails the channel.
Timeouts and uncertain access failures also fail the channel. A full command
ring before publication returns `-ENOSPC` without issuing a command. Invalid
arguments, unsupported versions and invalid channel states preserve all
outputs and perform no I/O. There is no automatic retry, power transition,
fallback OS-version read, handshake or reset.

`scripts/test_hfrp_power_state.py` compares all **256** response bytes against
the original `0xd41630` decoder, checks the exact native packet, and tests
v1.5/1.6/1.255 versus rejected older/other-major formats. It covers malformed
lengths, firmware errors, timeouts, wrong/duplicate sequences and a full ring,
plus **49** access-failure cases (including writes that take effect before
returning an error) and **18** argument/state rejections. Unlike the native
helper, the original accepts an empty response as zero state; that divergence
is checked explicitly. Native tests use the real C transport with a synthetic
RAM peer; original-code tests model the Windows sender. No physical state
transition or DMA retirement is established.

The bootstrap module exposes an explicit diagnostic `query_power_state` sysfs
write. It requires `CAP_SYS_RAWIO`, `enable_hfrp=1`, the supported machine,
the DLA endpoint, successful HFRP preparation and a READY channel. It shares
`boot_lock` with startup and inference and uses the PM/module lifetime already
retained by preparation. Startup, recovery and the qualification runner do not
invoke it automatically.

After loading the updated module and completing the documented preparation,
request one sample explicitly:

```sh
printf 'query\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/query_power_state
sudo cat /sys/bus/platform/devices/NVDA200A:00/hfrp_status
```

`query_attempted`, `query_error`, `query_valid`, `query_raw`, `query_reply`,
`query_length` and `query_payload` describe the last accepted diagnostic
attempt. Reading status only returns cached data; it never sends a command.
The query has separate result storage, preserving the original preparation
and power-command evidence. Errors invalidate the query snapshot; a failed
channel rejects further attempts without I/O. Do not clear that channel in
software to retry. The module logs each accepted attempt with its result.
An older loaded module does not gain this attribute when the source is rebuilt.

`scripts/test_linux_hfrp_state.py` compiles the actual store/show handlers with
mocked OS and transport boundaries. It checks privilege, endpoint, preparation
and channel gates, serialization, successful/repeated explicit requests,
errors, preservation of preparation evidence, and cached status reads. The
native transport test above covers packet and version validation separately.

A snapshot is neither proof of engine idle nor authorization to free task
buffers. Physical query qualification and the secure-firmware reset/drain
contract remain separate work.

## HFRP version discovery before power commands

Original function **`0xd3da30`** starts with HFRP handshake `0xd4af88`, then
reads **resource id 2, offset 0** through the original resource accessor
`0xd39878`. The returned u32 selects a fixed version or a version query:

| Register value | Windows action |
| --- | --- |
| Below `0x20250211` | reject with `0x56` |
| `0x20250211..0x20250424` | store major 1, minor 3 |
| `0x20250425..0x2025fffe` | store major 1, minor 5 |
| At least `0x2025ffff` | send command **999 (`0x3e7`)**, no request payload |

On transport success, command 999's four-byte response supplies major in bits
16..23 and minor in bits 8..15. The bytes are stored at link +0xc/+0xd, exactly
where the power callback reads them. The date-like constants above are exact
numeric comparisons; no build-date interpretation is required for the decoder.

`scripts/test_hfrp_version.py` runs the complete original discovery function
in 22 cases, including boundary values, the captured P16 stamp, response extraction and injected
failures. It stubs the handshake, register accessor, query transport and OS
helpers. A separate read-only capture now supplies the real P16 stamp below;
the test itself performs no hardware access.
The handshake itself writes HFRP control registers, so running discovery is
not merely a read-only hardware query and is not enabled by this work.

The replay exposes error handling which a Linux implementation must improve:
Windows propagates handshake failure, but ignores the register accessor's
return status. Query transport failure leaves the prior version bytes while
the outer function returns success; firmware status and reported response
length do not gate extraction. Linux must reject failed/truncated responses
and avoid treating stale or zero-filled version bytes as an established ABI.

Reproduce the two additional tests without hardware access:

```sh
venv/bin/python scripts/test_hfrp_power_policy.py
venv/bin/python scripts/test_hfrp_version.py
```

These findings identify missing bootstrap dependencies, not a working power
backend. Bounded rings and a strict polled channel are now implemented below.
The remaining immediate work is integrating version discovery and power
commands with the correctly selected parent path and Linux platform owner. The Linux
bootstrap still sends no HFRP power/reset commands. Neither observed SROOT
upload rejection is explained or repaired by these offline results alone.

## Bounded Linux command and reply rings

`driver/n1x_hfrp.c` now provides `n1x_hfrp_queue_command()` and
`n1x_hfrp_read_reply()`. They use the existing ordered read32/write32 callback
interface and **are not linked into either live kernel module**. They do not
establish device power, reset or protocol mode. The caller must own and
serialize the HFRP transport, including IRQ consumers.

The original mode-0 initializer `0xd4aeb8`, producer `0xd4b3a0`, consumer
`0xd4b7f0` and byte-copy helpers establish this layout:

| Location | Ownership and purpose |
| --- | --- |
| `0x110`, byte 0 | host command head |
| `0x110`, byte 1 | host response tail |
| `0x188`, byte 0 | firmware response head |
| `0x188`, byte 1 | firmware command tail |
| `0x114..0x187` | 116-byte command ring |
| `0x18c..0x1ff` | 116-byte response ring |
| `0x100`, bit 1 | host doorbell via IRQ_IN_SET |

The four-byte little-endian header holds total byte length in bits 0..7,
sequence in bits 8..17, and command (request) or firmware status (reply) in
bits 18..27. The reply helper preserves the entire raw header, including upper
bits whose meaning has not been established. Sequence `0x3ff` denotes an
asynchronous reply and is rejected for synchronous request allocation.

The producer leaves one ring byte unused to distinguish full from empty.
Consequently, maximum request payload is 111 bytes. It validates both index
bytes, writes the packet, publishes the command head, then rings the doorbell.
Byte writes preserve neighboring bytes through aligned word reads/writes;
publishing the head preserves the response tail and upper client-offset bytes.
`-ENOSPC` performs no packet writes. Access failures stop immediately.

Publication is tracked explicitly. A failed head-pointer write may still have
reached the device, as may a failed doorbell write. The result distinguishes
attempted and confirmed publications; callers must retain uncertain requests
and must not automatically retry or reuse their sequence. No automatic retry,
rollback, command timeout or sequence allocator is supplied by these primitives.

The consumer reads **one** response per call and checks published availability
before copying. Its fixed 50-byte payload limit matches the original Windows
consumer's length check. It rejects invalid indices, lengths below four and
oversized responses; empty/incomplete frames return `-EAGAIN`. These cases do
not advance the tail. Successful copying is followed by tail publication;
failure of that final write leaves consumption uncertain and must not be
treated as successful delivery. Firmware status is returned separately from
transport success, and an async response is not mistaken for command completion.
The helper does not acknowledge IRQs or match responses to outstanding commands.

`scripts/test_hfrp_ring.py` executes the original ARM64 initializer, producer,
consumer and byte helpers against synthetic MMIO. **72 TX and 162 RX cases**
compare packet bytes, preserved surrounding register memory, index updates,
payload/status and original outstanding-bit release. They include unaligned
heads/tails, wrapping, empty/full boundaries, maximum sizes and async replies.
Additional tests cover two queued responses consumed separately, eight
malformed/incomplete cases, invalid arguments/indices and **48 access-failure
cases**, including writes which take effect before returning an error.
The original consumer is not run on malformed inputs; its behavior is not used
as the Linux safety policy. Existing IRQ tests still pass, and the updated
component builds against the running kernel's headers.

## Complete original handshake in the emulator

The same test now executes original handshake **`0xd4af88`**, including the
original poll helper **`0xd3d750`**, producer and consumer. Only register access,
delay, logging, memory/CFG boundaries are modeled. The modeled peer supplies
reset acknowledgments and replies; this is not physical firmware validation.

The observed sequence is:

1. Clear software sequence ownership and IRQ bookkeeping.
2. Write `1` to IRQ_IN_SET (`0x100`) and wait for its bit 0 to clear.
3. Wait for IRQ_OUT_SET (`0x104`) bit 1 to become set.
4. Drain replies, clear IRQ_OUT bit 1 through `0x10c`, then drain again.
5. Queue command **1**, sequence **1**, with five bytes **`00 00 cc dd 00`**.
   The complete packet is `09 01 04 00 00 00 cc dd 00`.
6. Ring IRQ_IN bit 1, poll/drain/ack until that sequence is released.

This is a transport handshake, not DLA firmware authentication. The poll helper
also recognizes software IRQ status/counter changes; the modeled tests exercise
the direct register path. Each timeout case executes 5,000 original delay-helper
calls with argument 10. No wall-clock unit or physical timeout duration is
inferred from this argument alone.

Five peer scenarios establish normal completion, missing readiness, missing
reply, a reset bit which never clears, and firmware status `0x123`. Missing
readiness/reply returns Windows status `0x65`. **The initial reset timeout is
logged and ignored**, and a nonzero status in the matching reply still allows
success. The handshake does not validate the modeled four-byte reply payload.
A Linux handshake must reject failed reset and nonzero firmware status instead
of copying this permissive behavior. Its pending-sequence lifecycle must also
remain explicit on timeout; a timed-out command is not proven canceled.

Reproduce without accessing hardware:

```sh
venv/bin/python scripts/test_hfrp_ring.py
venv/bin/python scripts/test_hfrp_irq.py
make -C "/usr/lib/modules/$(uname -r)/build" M="$PWD/driver" n1x_hfrp.o
```

No HFRP mapping, reset, bootloader update, new firmware upload or module reload
was performed. The transport owner and strict handshake below still require
platform integration before they can support the NPU's actual power-on path.

## Strict Linux handshake and synchronous channel

`n1x_hfrp_handshake()` and `n1x_hfrp_exchange()` now implement the polled
transport in the same C component. These are callback APIs, **not a newly
loaded driver or proof of NPU bring-up**. They remain outside both live module
link lists. The caller must first establish the correct mode-0 aperture,
device power and exclusive ownership, hold its lock across each call, and
prevent concurrent IRQ/ring consumption. They do not discover hardware mode,
process power-event IRQs or provide recovery from an external controller reset.

A zero-initialized `n1x_hfrp_channel` belongs to one separately established
device lifetime:

| State | Permitted operation |
| --- | --- |
| NEW | one initial handshake |
| ACTIVE | no reentrant operation |
| READY | one synchronous command at a time |
| FAILED | neither further exchange nor another handshake |

Handshake first selects circular mode (offset `4 = 1`), then writes the
recovered reset request, requires reset-bit clearance
and peer readiness, drains/counts startup replies, acknowledges only doorbell
bit 1, then sends the exact original command-1 packet. It accepts only a
matching sequence-1 response with firmware status zero. Unlike the original
Windows function, reset timeout or a nonzero firmware status fails the channel.
The raw reply is retained; no undocumented payload interpretation is imposed.
Startup draining is allowed only after this explicit reset with no outstanding
owned commands. It is not a general permission to discard application replies.

The successful handshake leaves the next sequence at 2. Exchanges allocate
from 0..1022, wrap after 1022, and retain the pending sequence through response
consumption and doorbell acknowledgment. Preflight draining rejects stale
synchronous responses before a new command is published. Unexpected or
duplicate synchronous replies fail the channel. A complete nonzero firmware
status returns `-EREMOTEIO` while leaving a successfully completed transport
READY; it does not authorize continuing a failed power-on sequence.

Timeout, malformed input, failed notification delivery and access errors move
the channel to FAILED. A pending/uncertain sequence is not recycled, and a late
reply does not reopen the channel. The APIs never automatically reset, cancel
or retry. Reinitializing the C struct alone is not hardware recovery; the
owning driver must separately establish quiescence before a new lifetime.
A verified full command ring remains retryable because no packet was published.
The producer reports that condition explicitly as `space_unavailable`; an
accessor or notification callback returning the same `-ENOSPC` errno is not
mistaken for a full ring.

Asynchronous sequence-1023 replies go to an explicit synchronous callback
after successful tail publication. A missing/failing callback fails the channel
and retains the offending reply instead of silently dropping a notification.
Callbacks cannot retain the temporary reply pointer or reenter the channel.
The transport drains before acknowledgment and rechecks afterward to handle
arrivals in that window. Other IRQ bits are preserved for the eventual device
owner; observing those events and detecting unexpected device resets remain
platform-driver responsibilities.

Every wait has a caller-selected limit of 1..5001 polls. Delays occur only
between unsuccessful polls, with an optional zero-delay mode. Each drain pass
is additionally capped at 29 iterations, preventing a continuously replenishing
peer from extending a call indefinitely. Invalid indices, incomplete published
frames and notification floods cannot cause unbounded parsing. Result fields
record reset progress, publication attempts, the latest acknowledgment attempt,
matching-reply validity and startup/async counts. **A valid copied reply alone
does not override a later transport error**; callers must check return value
and channel state before proceeding.

`scripts/test_hfrp_channel.py` compiles the actual C component and drives a
synthetic register/firmware peer. Its **31 scenarios**, **125 injected access
failures** and **22 invalid-argument/state checks** cover:

- Immediate and delayed reset/readiness/reply, including the last allowed poll.
- Reset/readiness/reply timeouts, firmware errors, duplicate/wrong/stale replies.
- Sequence wrap and exclusion of late replies after timeout.
- Async delivery, callback failure/reentrancy, notification floods and ACK races.
- Malformed and incomplete published packets, full-ring retry and errno ambiguity.
- Errors before and after a write's possible side effect, with no further I/O
  and no reuse of a failed channel.

All scenarios pass with UndefinedBehaviorSanitizer enabled. The existing
Windows differential ring/handshake tests and IRQ tests also pass, and the
expanded component builds against the running kernel headers. These are
software checks; no physical reset, firmware query or NPU inference occurred.

```sh
venv/bin/python scripts/test_hfrp_channel.py
CC='cc -fsanitize=undefined -fno-sanitize-recover=undefined' \
  venv/bin/python scripts/test_hfrp_channel.py
```

## Checked version discovery and power-on packet

`n1x_hfrp_discover_version()` requires READY and reads the stamp at offset 0.
It follows the recovered Windows thresholds: below `0x20250211` is unsupported;
before `0x20250425` selects 1.3; before `0x2025ffff` selects 1.5. Newer stamps
require command 999 with a successful, exactly four-byte response. The major
and minor bytes come from bits 16..23 and 8..15 respectively. Discovery clears
previous output on an accepted attempt and never marks an unsuccessful result
valid. Access errors and malformed replies fail the channel. A completed
firmware error leaves transport ownership intact, but the version invalid.
Argument/state rejection preserves output and performs no I/O.

`n1x_hfrp_power_on()` sends command 400 for Windows request mask 7: wire bits
`0x008c`, enabling clock, power domain and rail. Version 1.3 carries a third,
explicit caller-supplied PPS byte; major 1/minor >= 5 uses two bytes, matching
Windows. Other versions are rejected. This helper neither chooses a PPS value
nor installs the required startup configuration. Completion is not proof of
physical power state. These functions remain outside the live module lists.

`scripts/test_hfrp_startup.py` checks 40 stamp/response combinations through
the actual C rings and synthetic peer against original ARM64 version discovery.
For valid versions it compares power packets at four PPS inputs against the
original Windows policy. Additional checks cover unsupported versions, invalid
states, malformed response lengths, failed reads, firmware errors and timeouts.
It passes normally and under UndefinedBehaviorSanitizer. Test PPS values are
arbitrary differential inputs, not values approved for this hardware.

## Windows startup configuration recovered with Ghidra and radare2

Ghidra exports of KMD RVAs `0xd41b50`, `0xd456e0`, `0xd49900` and the mode
setter `0xd3dd08` were checked against ARM64 instructions in radare2. The
decompiler's prototypes are unreliable around shared stack-cookie helpers and
tail calls; the executable replay below supplies stronger evidence.

`scripts/test_hfrp_config.py` executes the original startup parent, mode setter,
SRAM builder, configuration function, VF decoder, tuning function and final
statistics callback. The original profile-3 constructor resolves the outer
callbacks. Registry, resource writes, handshake/version and HFRP transport are
stubbed and recorded. No device is accessed. The default tested order is:

1. Read `RmSocDlaHfrpMode`, default 1; the original setter writes resource 2,
   offset 4, value 1 for nonzero mode. Mode 0 performs no resource write.
2. Invoke handshake/version discovery for mode 1.
3. Send command 412 (`SRAM_HOLD`), one byte: 2 for nonzero HAL+`0x474`, else 1.
4. Invoke HAL+`0x200`, resolved to `0xd456e0`, which sends command 401 (`CONFIG`).
5. Query command 428 (`GET_VF_CURVE`) and decode its response.
6. If HAL+`0x490` (`RmSocDlaEnableVbios`) enables the policy, invoke HAL+`0x208` (`0xd49900`) to send
   command 408 (`SET_POWER_CONTROL_TUNING`) followed by command 407
   (`GET_POWER_DRAW_CAP`). Both names match NVIDIA's L4T command table; the
   four-byte request to 407 does **not** establish that it sets a power cap.
7. For major 1/minor >= 5, the final callback at `0xd419f0` invokes
   `0xd414a0` three times. It sends command 432 with payloads `00 00 01`,
   `01 00 01`, `02 00 01` and a 26-byte response buffer, then zeros the local
   `outer+0x328..0x4a7` area. The three selectors and final flag are proven;
   treating the flag as a firmware-side statistics reset remains an inference.
   The callback logs individual errors and continues, returning zero even when
   every query fails. All three requests are tested under transport and firmware
   failure, and the local buffer is confirmed cleared in both cases.

Command 401 has 14 bytes for the older branch and 26 for major 1/minor >= 5:

| Byte offset | Type | Source / observed interpretation |
| --- | --- | --- |
| 0 | LE u16 | HAL+`0x5a4`, power-gating delay in ms |
| 2 | LE u16 | HAL+`0x5a8`, rail-gating delay in ms |
| 4 | LE u32 | settings+`0xac`, entry frequency in kHz, or zero when policy disabled |
| 8 | LE u32 | settings+`0xb0`, exit frequency in kHz, or zero when policy disabled |
| 12 | LE u16 | HAL+`0x5a0`, clock-gating delay in ms |
| 14..17 | 4 bytes | zero in the extended branch |
| 18 | LE u16 | constant 1 in the extended branch |
| 20, 22, 24 | 3 LE u16 | zero-extended outer bytes `0x31f`, `0x322`, `0x325` |

The original registry/default slice `0xd44488..0xd44508`, starting with the
observed preceding `w22=1`, establishes clock/power/rail delays of **1/4/10 ms**.
Registry names are `RmSocDlaDelayFlcg`, `RmSocDlaDelayElpg` and
`RmSocDlaDelayDlarg`. Successful synthetic overrides are also tested. L4T
confirms the units and first 14-byte layout. This does not establish the
appropriate frequency or power settings for a physical upload attempt.

The VF response starts with a u16 count, capped to eight by Windows, followed
by pairs of u16 values. The first value is stored as the voltage field; the
second is multiplied by 1000 into a frequency field. With a synthetic curve
`(700,400), (850,900), (800,600)`, replay produces frequency bounds
400000..900000 and voltage bounds 700..850. A synthetic requested frequency of
650000 is selected within those bounds. No real device curve was queried.

Command 408 copies nine consecutive u32 words from settings+`0xc0..0xe0`
into a 36-byte payload. Command 407 copies settings+`0xbc` into a four-byte
payload. Their producers are now traced to the VBIOS BIT `l` directory; see
[the VBIOS parser and Linux decoder](hfrp-vbios.md). The actual board's values
and any applicable override policy remain to be obtained. The test uses
deliberately distinct synthetic words to establish field order only.

The replay covers 12 version/policy/SRAM combinations, mode overrides 0 and 2,
ten injected command failures and two delay-default/override cases. It also
establishes error-propagation defects: the parent discards configuration return
codes, CONFIG firmware errors can be logged and ignored, and tuning command
408 can fail while the next command still runs. These are observed Windows
behaviors, **not** rules to copy into a Linux driver. A future Linux startup
sequence must check every required command and exact reply layout.

```sh
venv/bin/python scripts/test_hfrp_startup.py
venv/bin/python scripts/test_hfrp_config.py
CC='cc -fsanitize=undefined -fno-sanitize-recover=undefined' \
  venv/bin/python scripts/test_hfrp_startup.py
```

Platform binding, full policy initialization and cold-start ordering relative
to SROOT remain unresolved. The trailing startup callback is now replayed above.
The retained DMA
from the failed physical upload was not released or reused. No reset, new
firmware upload, module reload, boot change or reboot was performed for this
work. The next step is to obtain the actual VBIOS policy and finish tracing
cold-start ordering, then review the complete startup sequence before a
separate hardware attempt.

## Live P16 stamp, read-only capture

On 2026-10-25, `scripts/read_hfrp_stamp.c` read the two u32 registers at
physical `0x31380000` and `0x31380004`:

```json
{"physical_base":"0x31380000","stamp":"0x20251722","mode":"0x00000001","register_reads":2,"register_writes":0}
```

The captured machine was ProArt P16 H7607BA, BIOS H7607BA.300, kernel
`7.2.8-23-omarchy-p16-asus-npu1`, boot ID
`413b4af3-8df3-4859-bf42-07eb6b7f0263`. The existing bootstrap driver owned
NVDA200A:00 and runtime PM reported active. This capture occurred **after**
the failed variant-1 SROOT attempt; it is not a pristine post-boot baseline.
The program requires that exact kernel/platform/BIOS, owner, active state and
ACPI resource range. It maps one page read-only through `/dev/mem` and performs
exactly two volatile u32 loads. It does not bypass `/dev/mem` protections,
change runtime PM, reset, handshake or write registers. It is a fixed research
diagnostic, not a generic discovery tool or an installed service.

The Windows numeric selector maps `0x20251722` to **1.5**, without command 999.
This is version selection from a real stamp, not a successful transport
exchange. The value is not interpreted as a calendar date. Both the original
selector test and the native Linux startup test now include this captured
input; their handshake/reply peers remain synthetic. The read does not show
whether the HFRP controller has completed its startup sequence or whether the
NPU firmware is running. No retained DMA was freed or reused.

## Cold-start dispatch chain

Ghidra and radare2 resolve the previously opaque `outer+0x178` object. The
HAL allocation constructor `0x195d038` places an interface at `HAL+8`, stores
that address at `HAL+0x68`, and installs vtables at RVAs `0x108e9a0` and
`0x108e9b8`. The resource initializer copies `HAL+0x68` to `outer+0x178`.
The interface thunks subtract eight from their HAL argument and dispatch
through the main table:

| Outer caller | Interface slot | Thunk RVA | Main slot | Target RVA / role |
| --- | --- | --- | --- | --- |
| `0xd39b98` | `0x1d8` | `0x5eee00` | `0x258` | `0xd440d0`, registry/default initialization |
| `0xd39d38` | `0x1e0` | `0xd3c1b0` | `0x260` | `0xd44710`, gating policy initialization |
| `0xd39c08` | `0x1f0` | `0x142380` | `0x270` | `0xd44640`, start through HAL callbacks |
| `0xd39c08`, field `outer+0x310 == 3` | `0x200` | `0x142410` | `0x288` | `0xd43730`, additional setup branch |

`0xd39d38` first invokes `outer+0xb0`, the HFRP startup at `0xd41b50`,
then gating initialization. `0xd39c08` normally invokes the profile-3 no-op
at `outer+0xb8`, then the start target. When field `outer+0x310` equals 3,
it first invokes HFRP startup again and `0xd43730`. A producer is now replayed
in [the power-state transition path](#power-state-transition-producer); the
actual complete cold-start baseline remains unverified. This state must not be
confused with the separately established device-profile value 3.

For HAL 1, `0xd44640` calls HAL+`0x3d8` (a no-op), then HAL+`0xc8`
(`0xd47700`). The latter invokes the outer power callback `0xd405f0` with
request mask 7 and guard 0, and only after success enters common loader
`0xd439c0`. Power failure prevents the loader. Either power or loader failure
causes `0xd44640` to invoke HAL+`0xd0` (`0xd4a4f0`) and preserve the original
error. Its cleanup body is not validated by this dispatch test and must not
be copied as a safe Linux recovery operation.

`scripts/test_cold_start_dispatch.py` executes the original profile/HAL
callback constructors, wrappers, interface thunks, start target and HAL-1
loader wrapper. It uses the original vtable bytes and reconstructed object
bookkeeping. Sixteen cases verify ordering, missing runtime objects, both
values of the conditional branch and error propagation, including suppression
of the loader after power failure. Registry, HFRP startup, gating/additional
setup, power command, common loader and cleanup bodies are explicit recording
stubs. This complements their separate tests; it is not an integrated boot.

The additional setup body is independently covered by
[`emulate_region5_setup.py`](../../scripts/emulate_region5_setup.py), which executes
`0xd43730` and its actual allocation callbacks. HAL slot `+0x150` resolves to
`0xd45250` (clear an allocation), `+0x430` to `0xd45390` (prepare the low-power
configuration page), and `+0xa8` to `0xd45300` (prepare a zeroed configuration
page). Constructor defaults allocate the 1-MiB region-5 buffer and two 4-KiB
pages. The low-power record has a separate Windows/C comparison; see
[region-5 setup](region5.md) and
[command-23 construction](contract.md#original-command-23-payload-construction).
These existing tests establish the additional setup body's role without
assuming the three allocations are new or unexplained firmware requirements.

The global initializer `0xd38a88` calls `0xd39b98`, `0xd39d38`, then
`0xd39c08`. ARM64 instructions at `0xd38e14` show that the return from
`0xd39d38` is discarded before calling `0xd39c08`. This outer error handling
is static evidence, not covered by the dispatch replay. A Linux sequence must
check required startup/policy operations rather than reproduce that omission.

```sh
venv/bin/python scripts/test_cold_start_dispatch.py
venv/bin/python scripts/emulate_boot_policy.py
```

Board VBIOS values, the conditional setup producer and a complete reviewed
physical startup path remain outstanding. The policy initializer is now
replayed in the following section; its hardware-facing setup callbacks remain
stubbed and a complete Linux startup translation is still needed.

## Complete policy initialization and final gating mask

`scripts/test_hfrp_policy_init.py` now runs the whole original `0xd440d0`,
its feature updater `0xd435f8`, and mask producer `0xd44710`, rather than
isolated registry slices. Original profile/HAL callback constructors, default
field sequence, stack-cookie code and owner lookup also execute. The chip
getter returns the established synthetic input `0x254`. Registry reads are
stubbed by name; remapper-install and allocation/setup callbacks are recording
boundaries. No hardware registers, real mappings or firmware are accessed.
Settings inputs come from the separately executed Windows VBIOS parser.

The settings-to-gating mapping is now established end to end:

| Source | Result before feature updates |
| --- | --- |
| settings+`0xa0`, frequency table's first flag byte | outer+`0x31d` is 1 when source is zero; +`0x31e` is 1 |
| settings+`0xec`, gating table's flag byte | outer+`0x320` and +`0x323` are 1 when source is zero; +`0x321` and +`0x324` are 1 |
| settings+`0xf8` | replace zero with 1, copy low byte to outer+`0x31f` |
| settings+`0xfc` | replace zero with 3, copy low byte to outer+`0x322` |
| constant | outer+`0x325` is 15 |
| settings+`0xf4` | replace zero with 4 in settings; no additional meaning inferred |
| settings+`0x94`, +`0xbc` | copied to HAL+`0x590`, +`0x594` respectively |

The newly decoded VBIOS general feature mask at settings+`0x80` is then
applied, followed by an optional `RmSocDlaFeature` registry mask. Each call
processes bits from low to high and mutates existing state. Zero bits do not
reset fields. The following subset affects the HFRP policy directly; other
bits affect HAL flags and must not be discarded in a future full translation:

| Feature bit (zero-based) | Observed effect |
| --- | --- |
| 1 | set outer+`0x31d` to 1 |
| 2 | set outer+`0x14c` SLCG override flag to 1 |
| 3 | set outer+`0x320` and +`0x323` to 1 |
| 4 | set outer+`0x323` to 1 |
| 17 | clear outer+`0x31d` |
| 18 | clear SLCG override flag |
| 19 | clear outer+`0x320` |
| 20 | clear outer+`0x323` and, by fall-through, +`0x320` |

`0xd44710` produces mask 7, plus `0x110`, `0x220`, `0x440` when respectively
`31e && !31d`, `321 && !320`, `324 && !323`. If the global owner's byte +`0x58`
is nonzero, it instead sets mask zero. The associated registry path is named
`RmSocSocOsPresilTesting`; a production interpretation of that owner byte is
not established by its spelling. This is a request-permission mask, not
physical readback of clock or rail state.

Full initialization also overwrites the earlier `RmSocDlaRemapper` result at
`0xd4455c`: HAL+`0x478` becomes 1 for owner+`0x58 == 0`, otherwise 0. The
recorded installer boundary sees this final value. Registry overrides 0, 1,
2 and `0xffffffff` all end at 1 for the normal owner. Previous slice tests
remain useful to describe the intermediate parser, but not effective startup
configuration. The physical experiment's mode-1 choice remains consistent.

The test covers eight source-flag/owner combinations, all 32 single feature
bits under both enabled and disabled source flags (each compared between
VBIOS and registry paths), three ordered/conflicting feature combinations,
four remapper overrides, allocation/setup failure propagation and absent
settings. It verifies delays 1/4/10 ms and the 4/1/3 zero-value substitutions.
Absent settings yield mask 7 and zero outer policy bytes in this synthetic
case; that does not justify omitting the real board policy. Allocation/setup
bodies, tuning-override handling and actual registry/VBIOS contents remain
outside this replay.

```sh
venv/bin/python scripts/test_hfrp_policy_init.py
venv/bin/python scripts/test_hfrp_vbios.py
```

## Native policy setup

`n1x_hfrp_configure_policy()` in `driver/n1x_hfrp_setup.c` implements the
normal HAL-1, VBIOS-enabled configuration sequence on the bounded channel:
SRAM_HOLD (412), CONFIG (401), GET_VF_CURVE (428), tuning (408), cap query
(407), then three statistics queries (432) for version 1.5 or later.
Version 1.3 sends five commands; 1.5+ sends eight. Packet fields match the
original Windows startup replay, including default delays and prepared PPS
values. The cap query's request word is not treated as proof of a power-limit
setter. No registry overrides or pre-silicon behavior are implemented.

The caller must supply a complete policy from the matching board, establish
ownership/power and serialize the channel after handshake/version discovery.
This helper does not perform those steps, power-on, firmware upload or platform
binding. It is compiled as an isolated object and is not linked into either
live module. Gating/HAL bookkeeping outside the transmitted fields and the
Windows software frequency selection after VF discovery remain separate work.

**Reply lengths are inferred host-side contracts, not verified P16 firmware
responses.** The helper requires SRAM 1 byte, CONFIG 14/26 bytes, tuning 36
bytes, cap query 4 bytes and statistics 26 bytes. VF replies accept either
`2 + 4 * count` or 34 bytes with 1–8 entries. It preserves each raw voltage
word and multiplies the frequency word by 1000 as Windows does. Empty,
oversized and truncated curves fail instead of selecting an invented fallback.
Windows buffer sizes and L4T parsers motivate these bounds but do not establish
the actual wire lengths; hardware qualification must resolve this limitation.

Every command checks transport, firmware status and response shape. Failure
stops subsequent commands and marks the channel FAILED, preventing automatic
replay after partially applied configuration. The result records the command,
completed prefix and last exchange. Invalid inputs preserve caller state and
perform no I/O. `configured` only means this sequence completed under the
inferred contracts, not that calibration or firmware boot is verified.

`scripts/test_hfrp_setup.py` compares 18 outgoing packet sequences with the
original Windows code in Unicorn, using synthetic policies and VF curves.
It tests 35 command/response failures, 771 injected I/O failures, 16 preflight
rejections, VF count boundaries and sequence rollover. Both normal and UBSan
runs pass; the object builds against `7.2.8-23-omarchy-p16-asus-npu1` headers.
The original configuration replay also passes after adding configurable
synthetic settings. All device responses and MMIO effects are simulated.

```sh
venv/bin/python scripts/test_hfrp_setup.py
CC='cc -fsanitize=undefined -fno-sanitize-recover=undefined' \
  venv/bin/python scripts/test_hfrp_setup.py
make kernel-check
```

## Power-state transition producer

The profile-3 callback constructor installs `0xd40d50` at `outer+0xf8`.
Ghidra and radare2 show that this function writes `outer+0x310`; executable
replay confirms the following fields and ordering:

| Field | Observed role |
| --- | --- |
| `outer+0x310` | current numeric state, read by the start branch |
| `outer+0x314` | previous state, copied when a transition finishes |
| `outer+0x318` | requested state, stored before the transition callback |

Equal-state requests return `0x3f` without modifying these fields. A positive
old state also rejects any nonzero target. The comparison is signed and does
not validate an enum; negative synthetic values expose that distinction.

For a target of zero, policy-mask bit 5 calls the actual start wrapper
`0xd39c08`. It still sees the **old** state. Thus a transition **3 -> 0**
executes HFRP startup, additional buffer preparation, power request 7 and the
common loader, subject to the existing runtime-object checks. Other old
states omit the first two steps. If bit 5 is clear and bit 4 is set, the
function instead calls the outer power callback with guard 0 / request 1.
Bit 5 takes precedence when both are set.

For zero -> nonzero, bit 5 calls stop wrapper `0xd39de0`; otherwise bit 4
issues power request 16. The stop wrapper separately tests requested state
`outer+0x318 == 3` for an additional teardown callback. Its internal teardown
is not exercised by this transition test. After storing current/previous
state, a nonzero target optionally invokes `outer+0x120` / `0xd40e90` when
byte `outer+0x14a` is set. This notification's return value is discarded.

**Windows updates current/previous state even when a start or stop operation
returns an error.** The tested 3 -> 0 path therefore leaves current state 0
after HFRP startup, allocation-setup, power or loader failure. This is evidence
about the Windows implementation, not a recovery rule for Linux: software
must not mark failed hardware transitions successful or retry retained DMA.

`scripts/test_dla_power_state.py` runs the original state writer, actual
callback constructors and start dispatch for 400 state/mask/runtime/notification
combinations plus 11 failure cases. It checks callback-visible state, final
state, power masks and suppressed downstream operations. HFRP, allocation
setup, power, loader, cleanup, stop and notification bodies remain explicit
stubs. The existing 16-case cold-start dispatch replay still passes.

This resolves one producer of the formerly unexplained branch condition and
supports a resume interpretation. It does not independently identify numeric
state 3 as a Windows or ACPI enum, prove the complete cold-start state, or
establish safe suspend/resume on the P16. The outer allocation constructor
clears its 0x4d0-byte object, but later callbacks still need to be included in
a complete cold-start replay before relying on a zero baseline.

```sh
venv/bin/python scripts/test_cold_start_dispatch.py
venv/bin/python scripts/test_dla_power_state.py
```
