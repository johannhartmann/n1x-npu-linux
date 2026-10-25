# HFRP and firmware IRQ evidence

The auxiliary P16 aperture has a verified Windows HFRP register/interrupt
protocol correspondence with the pinned L4T source. **The physical P16 GSI
assignment is still unverified.** No Linux IRQ is registered by this work, and
the bootstrap still does not map/write the auxiliary aperture. HFRP response
rings, clock/power transitions and recovery are not implemented by the new
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
is invoked to schedule subsequent work. A primary accessor error prevents that
accumulation/scheduling. The original 48-case parent test uses explicit primary status/scheduling
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
`0xd3b4a0(outer, parent->0x160, 0)` and returns 1. That final helper is an
explicit test boundary; its notification/synchronization semantics are not
reconstructed here. The tested input register x0 does not affect this path.
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
  `+0x4a0`. A failed accessor neither accumulates nor schedules.
- `0xd45670` follows primary `+0x68` to the provider, reads its ownership mask
  at `+0x54`, ANDs it with accumulated `+0x4a0`, then calls provider vtable
  **+0xb0** with a pointer to the filtered u32. The provider callback is an
  explicit test boundary, not evidence that Windows work/IRQ delivery occurred.

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

The mode's low byte is used: input 256 reproduces mode 0. Its caller-level
meaning is not yet established. Missing links or a capability value other
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
that outer callback's implementation and the actual parent selection still
need tracing. Do not equate outer +0xc8 with a primary HAL slot.

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
