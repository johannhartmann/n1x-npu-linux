# Windows KMD task submission and postaction construction

Research date: 2026-10-27. `scripts/inspect_kmd_task_submission.py` executes
original ARM64 instructions from NVIDIA 617.42 `nvlddmkm.sys`, SHA-256
`73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987`.
Addresses below are RVAs in that image (preferred base `0x140000000`).
This is offline evidence, not a physical submission or a Windows OS trace.

## Executed submit chain

The tested path is:

```text
0xdd6888  ring-address calculation and engine selection
0xdd22c8  control envelope: command 0x35800001, 16-byte parameters
          [generic RM object lookup modeled using the checked method table]
0xd45050 command handler, primary +0x140, flags 4
0xd4a400 task method wrapper, primary +0xf8, command 7
0xd49fc0 direct method sender branch
          [low-level sender 0xd4c800 modeled]
0xd45180 original pending-record cleanup
```

The original HAL-1 initializer `0x1951238` installs `0xd4a400` and
`0xd49fc0`; the test does not supply those function pointers by assumption.
Method-table record `0x108e7c0` identifies command `0x35800001`, input size
16 and handler `0xd45050`.

`0xdd6888` obtains its context through a device virtual callback and calls a
descriptor-preparation callback. Those two callbacks remain synthetic. After
successful preparation, the original code calculates:

```text
sequence = job.u32[0x2c]
descriptor = submitter.u64[0x28] + ((sequence & 31) * submitter.u32[0x18])
```

The offset multiplication is 32-bit. The engine selector comes from the
indexed engine record, using `job.u32[0x20]`, a `0x170` record stride and
field `+0x38`. Twelve cases execute both supported selector values and ring
indices 0, 1, 31, 32, 33 and `0xffffffff`, checking the original path through
the final sender. These are constructed objects. The producer of the job
record is now also executed, as described below; the complete OS scheduling
and notification path remains open.

`0xdd22c8` builds this control header:

| Offset | Value |
| --- | --- |
| `0x00` | 32-bit client from object `+8` |
| `0x04` | Object handle: mode 1 -> `0x35800000`, mode 2 -> `0x35800001` |
| `0x08` | Command `0x35800001` |
| `0x0c` | Flags zero |
| `0x10` | Pointer to two QWORDs: descriptor address, sequence |
| `0x18` | Parameter size 16 |
| `0x1c` | Status, initialized to `0x56` |

Its 40-byte context is `<u32 2, u32 1, u64 object[0x10], 24 zero bytes>`.
A nonzero global at `0x149ba3c` prevents dispatch and returns false. Otherwise,
the wrapper returns whether the header status is zero. The direct wrapper
preserves a 64-bit sequence; its tested higher-level caller loads only 32 bits.
Missing primary state returns handler status `0x40` and wrapper false.

The model replaces `0xdd1c10` with dispatch to the checked table entry and
copies the returned handler status into the control header. It does not claim
to execute RM registry lookup, WDDM submission callbacks or OS scheduling.

## Method encoding and errors

The original handler passes flags 4. The HAL wrapper sends command 7 with
32-bit data `(descriptor >> 8)`, and the sender encodes the flags as method
**`0x207`**. It increments primary counter `+0x5b4` before calling the
low-level sender. The tested branch has parent `+0x4a8 == 0`, successful
synthetic locks and no pending records at primary `+0x5bc`.

The low-level sender's return value is discarded in this direct branch.
Injecting status `0x65` still gives handler status zero, wrapper true and an
incremented outstanding counter. The subsequent original `0xd45180` only
checks pending-record bookkeeping here; it does not wait for firmware or
establish completion. The counter is not a task-specific retirement fence.

The wrapper also discards low address bits and bits above the 32-bit encoded
value. Tests record that behavior; it does not justify accepting unaligned or
out-of-range Linux addresses. The native executor already rejects these.
Its qualified method is `0x307`, with its separately tested reply/fence/SWGEN0
checks. Do not change it to `0x207` merely to copy this asynchronous Windows
submission branch.

## Vendor postaction producer -> original firmware consumer

Original function `0xdd6a08` constructs a completion postaction using the same
`job.u32[0x2c]` value. It follows:

```text
submitter +0x20 -> adapter +0x6ec0 -> state +0xed8
    -> provider +0x18 -> fence owner F
```

With `Q = F.u64[0x4e0]`, it passes
`Q.u32[0xf00] * job.u32[0x20]` to the original index helper `0xdd7708` at
`F+0x508`. That helper uses base index `+0`, count `+4`, byte stride `+8`:

```text
index = base_index + (stride_bytes >> 2) * input_index  [32-bit arithmetic]
fence_address = F.u64[0x4e8] + u32(index << 2)
```

For postaction table offset `T = descriptor.u16[0x12]`, the builder writes
the table entry `<u16 ((T+0x1a5)&0xffff), u16 14>`. At that action offset it
writes exactly:

```text
80 | fence_address:u64 little endian | sequence:u32 little endian | 00
```

This is the recovered `WRITE_SEM` plus terminator already used by
`n1x_dla_build_fenced_task_v2()`. The test compiles that actual native C builder
and verifies byte-identical action streams for each producer fixture.
Windows places this list at a different
offset; this is not a claim that the complete Windows and Linux descriptors
are byte-identical. The helper returns 1 without changing the postaction count.
Its caller at `0xdd75ac` sets byte `+0x0f` after that return. The test performs
that one-byte update explicitly; the complete descriptor builder is not run.

Twenty-four cases feed the actual emitted descriptor/list to original HAL-1
firmware caller `0x103d56` and its postaction interpreter, with RAM-only DMA.
The firmware image is pinned to SHA-256
`2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997`.
For task status zero, the firmware writes the expected 32-bit sequence. For
nonzero task status, it performs no postaction DMA and leaves the semaphore
unchanged, while the postaction helper itself returns zero. Thus neither a
successful submission return nor this helper's zero return means inference
success. A separate producer-only case preserves a fence address above 4 GiB.

The test covers valid index arithmetic, not the producer's invalid-index
assertion behavior. It does not execute engine arithmetic, the complete task
scheduler, Windows completion notification or physical DMA/cache ordering.

## Correction to the earlier static lead

`0x19d87d0` also selects `0x35800001`, but that value is the **object handle**
in argument W2. Its command in W3 is **`0x3580000a`**, with an 8-byte payload.
It is not the task-submit entry. The actual submit envelope builder is
`0xdd22c8`, reached from `0xdd6888` at call site `0xdd6980`.

## Linux implications and remaining boundary

The vendor producer independently supports our explicit 32-bit completion
semaphore encoding and unique per-job values. It does not establish that
observing the semaphore makes all descriptor, model and output DMA backing
safe to free. Keep the native retention/fail-stop policy until hardware
repetition, engine retirement and reset or another quiescence guarantee have
been qualified. The producer now distinguishes `job+0x2c` from the OS fence
stored at `job+0x30`. Trace the corresponding completion notification next to
connect these sequences to the OS lifetime contract described in
[windows-buffer-lifetime.md](windows-buffer-lifetime.md).

## Job producer: internal sequence versus OS fence

Original function `0xdd28f8` creates the `0x1f0`-byte job record. The test
executes its complete entry and return for an empty nonpaging context
submission. Adapter/context getters, spinlock OS calls and final dispatch
are modeled; the indexing, increments, record clearing and population,
dispatch-error marking and return execute unchanged. The real dispatch
pointer `0xdd5ab0` is present at `0xe9c930`; that large dispatch body is not
executed by the producer test.

The input layout matches Microsoft's 64-bit
[DXGKARG_SUBMITCOMMAND](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkarg_submitcommand):
context at `+0`, submission offsets at `+0x1c/+0x20`, private-data pointer at
`+0x28`, `SubmissionFenceId` at `+0x3c`, flags at `+0x48`, virtual address at
`+0x50`, and node ordinal at `+0x58`. This layout correspondence identifies
the OS fence field; the test does not replay registration or invocation of
the public WDDM DDI itself.

With scheduler state `S` and `q = node * 8 + engine`, the producer performs:

| Job field | Source and behavior |
| --- | --- |
| `+0x20`, `+0x24` | Node and engine from the context |
| `+0x28` | `++S.u32[0xf98]`, global submission counter |
| `+0x2c` | `++S.u32[0xf9c + 4*q]`, queue-local sequence |
| `+0x30` | Input DWORD `+0x3c`, the OS `SubmissionFenceId` |
| Record address | `S.u64[8 + 8*q] + (sequence % S.u32[0x7d8 + 4*q]) * 0x1f0` |

The later DLA submitter uses `job+0x2c` for its separate 32-slot descriptor
ring and firmware semaphore. The OS fence value can differ arbitrarily;
it must not be conflated with that internal sequence. Forty-eight cases
vary queue, OS fence, internal wrap and final-dispatch status. Both counters
remain advanced after a negative dispatch return, and job flags gain `0x18`.
This does not demonstrate cancellation or permit Linux to roll a sequence back.
The Windows queue counter wraps at 32 bits; our native no-wrap policy remains.

Microsoft's [completion notification contract](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkargcb_notify_interrupt_data)
reports the OS submission fence. Consequently, tracing the firmware value
without also recovering this translation is insufficient to explain the OS
completion boundary.

## Preactions distinguish entry, permission to run and completion

Original builder `0xdd6ab0` emits one 40-byte preaction list. The engine
property lookup is modeled; arithmetic helper `0xdd7708`, all serialization
and the disabled branch execute unchanged. Engine property bit 2 disables
the entire list and leaves the descriptor untouched. Otherwise, the list
starts at `postaction_table_offset + 4`; its table entry is written at the
descriptor's preaction-table offset (`u16 +0x10`). The caller sets the count.

The emitted order is:

1. `0x92`: require the completion semaphore to have reached `sequence - 1`.
2. `0x80`: write the current sequence to a separate entry/start semaphore.
3. `0x90`: require the control semaphore to equal `0xfeed`.
4. `0x00`: terminate the action list.

The semaphore banks all use device base `F.u64[0x4e8]`, but different index
descriptors and multipliers:

| Role | Index descriptor | Index input for this DLA builder |
| --- | --- | --- |
| Completion, also written by `0xdd6a08` | `F+0x508` | `Q.u32[0xf00] * node` |
| Entry/start marker | `F+0x52c` | `Q.u32[0xefc] * node` |
| Control gate | `F+0x520` | `Q.u32[0xefc] * node` |

Sixteen cases feed the actual Windows-produced stream to original RV32
interpreter **`0x1030ae`**. With the previous task complete and gate `0xfeed`,
it returns zero and writes the entry marker. With the previous task still
pending it returns `-13` without that write. With the previous task complete
but gate `0xf00d`, it **writes the entry marker and then returns `-13`**.
The marker therefore does not prove engine execution, much less completion.
The interpreter's `0x92` predicate uses signed 32-bit subtraction; the cases
include crossing zero and `0x80000000`. Pending predicates are immediate
interpreter results here, not a simulated scheduler wait or timeout.

For successful preconditions, the test subsequently passes the real Windows
postaction stream through the original postaction interpreter and observes
only then the completion semaphore advancing. There is deliberately no
arithmetic engine or scheduler between those two invocations. The test
establishes distinct marker semantics, not inference or retirement.

## Control-gate writer

Original function `0xe18220` uses `0xdd1548` to obtain the CPU mapping at
`F+0x4f0` and `0xdd1470`/`0xdd7708` to select the `F+0x520` gate bank. The
writer uses `node * Q.u32[0xefc] + engine`, while the DLA preaction builder
above selects engine zero. It writes `0xfeed` when the low byte of its fourth
argument is zero and `0xf00d` otherwise, executes `dmb st`, then releases
the lock. The blocked branch also calls `KeSetEvent` for two driver events.

Four cases execute that writer plus its original lock/release/event wrappers.
Only the imported OS calls are modeled; the imports are checked against the
pinned PE: `KeAcquireSpinLockRaiseToDpc`, `KeReleaseSpinLock`, `KeSetEvent`.
This proves a software submission gate. It contains no demonstrated wait for
engine idle and must not be used alone as a Linux reset/quiescence procedure.

Remaining static leads: `0xe183b0` and `0xe18a30` inspect job records and copy
the separate field `job+0x30` into result state. Both call `0xdd1428`, whose
index descriptor is `F+0x52c` (the entry marker), so treating those reads as
completion reads would be incorrect. Accessors `0x92e30` and `0x92f10` read
and write a different cached sequence at state `+0x2e9c + 4*engine + 32*node`.
The raw completion-bank path to `DxgkCbNotifyInterrupt` is now executed as
described below. Its relationship to this separate cache, deferred cleanup,
OS scheduling and actual memory reclamation remains open.

```sh
venv/bin/python scripts/inspect_kmd_task_submission.py > results/kmd-task-submission.json
```

The 127 cases comprise 16 direct submissions, two global gates, missing-primary
handling, two address-encoding probes, 12 higher-level ring submissions,
24 postaction-consumer cases, one 64-bit postaction-address case, 48 job
producer cases, 16 preaction-consumer cases, one disabled preaction producer
and four control-gate writers. All run offline; no device, boot configuration
or register is changed.

## Completion-bank selection and OS notification

`scripts/inspect_kmd_completion.py` executes the original ARM64 completion
reader and notification path using the same pinned KMD image. The tested chain
is:

```text
e22500   read completion value; select matching job ring record
  dd1548   CPU semaphore mapping F+4f0
  dd15b0 -> dd7708   completion-bank index F+508
  e218b8   selected-job accounting and notification
    e05cf8 -> e07010   OS notification wrapper and metadata extraction
      DXGKRNL_INTERFACE+80   modeled DxgkCbNotifyInterrupt
```

For the memory-backed branch (`object.u8[0x480c] != 0`), the selector reads
`F.u64[0x4f0] + 4 * index`, where:

```text
input = Q.u32[0xf00] * node + engine
index = F.u32[0x508] + (F.u32[0x510] >> 2) * input
q = node * 8 + engine
previous = scheduler.u32[0x137c + 4*q]
record = scheduler.u64[8 + 8*q]
       + ((next_sequence % scheduler.u32[0x7d8 + 4*q]) * 0x1f0)
```

This is the CPU view of the completion bank used by the postaction producer,
not the `F+0x52c` entry marker. The initial reader accepts a modular forward
distance below 32; it does not notify for equal, stale or distance-32 values
in the tested policy. For invalid deltas, tests explicitly set capability bit
13 in `object[0x1c8]+4` to skip recovery; other error/recovery branches are not
executed. The job's owner and sequence are checked before retirement; bit 10
in `job+0x1e8` stops processing without advancing the completed sequence.

The alternate branch (`object.u8[0x480c] == 0`) uses the submitted sequence
at scheduler `+0xf9c+4*q`, even when the raw completion word is older. It is
tested as a distinct software policy, not adopted as hardware completion
evidence for Linux. Its activation on a live DLA adapter is unproven.

`e218b8` decrements outstanding counters, records a completion timestamp and,
unless `job.flags & 0x18`, performs the optional job callback and then records:

- `scheduler+0x26dc+4*q`: the independent OS fence from `job+0x30`.
- `scheduler+0x1f1c+4*q`: the internal sequence from `job+0x2c`.
- An OS interrupt notification of type 1 (`DMA_COMPLETED`), carrying that OS
  fence, node and engine. The notification flags are zero on this path.

The original wrapper obtains the OS interface through `object+0x1988`, reads
its device handle at `+8` and calls callback `+0x80`. This layout matches
Microsoft's [DXGKRNL_INTERFACE](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/dispmprt/ns-dispmprt-_dxgkrnl_interface)
and [notification data](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkargcb_notify_interrupt_data).
Only the actual OS callback, getters, clocks and logging are synthetic. The
wrapper sets `object+0x4b48` after the callback with a release store/barrier.
Neither the notification nor the tested counter update frees the job record.

The 166 passing cases comprise 48 selected-job notifications, 70 selector
cases, 32 additional class `(5,2)` cases and 16 linked producer/firmware/reader
cases. Class `(5,2)` is checked by the DLA dispatcher `dd5ab0`; the additional
cases verify that the shared completion behavior does not depend on the
constructed class `(1,1)` used to cover optional graphics-side branches. They
cover both the empty producer's type 6 and the virtual-submit translator's
type 1 job described below.
They cover sequence zero,
ring wrap, the signed boundary and `0xffffffff`, distinct OS fences, queue
indices, suppression flags, pending/stale/out-of-window completion values,
missing CPU mapping, the alternate policy and the bit-10 processing stop.
The linked cases execute the original Windows postaction producer, feed its
descriptor to the original RV32 postaction caller, then copy the resulting
RAM completion word into the original Windows reader's CPU mapping. Successful
task status advances it and notifies; failed status skips the postaction and
produces no notification. No arithmetic engine or physical DMA is modeled.

Static call-site follow-up found `e22500` passed to `dca2b0` at `e1aad0`,
`19e1cfc` and other sites. That wrapper dispatches through OS-interface `+0x48`
(`DxgkCbSynchronizeExecution`); it is not executed by this test. Separately,
`e21e20` advances scheduler `+0x1b3c` and calls `e21448`. This deferred cleanup
path is now executed separately below; it is not an allocation-destruction
boundary by itself.

For Linux, this strengthens the interpretation of the unique firmware fence
and its distinction from acceptance/IRQ acknowledgment. It does **not** prove
that all device DMA is quiescent, that Windows memory destruction is immediate,
or that our native driver may now free retained images. Physical repeated
execution, coherent observation and safe reclamation/reset remain required.

```sh
venv/bin/python scripts/inspect_kmd_completion.py > results/kmd-completion.json
```

## Deferred cleanup and APC dispatch

`scripts/inspect_kmd_cleanup.py` executes full original functions `e04fd0`,
`e21e20` and `e21448`. The first is a normal walker over nodes/engines. With
memory-backed completion enabled, an available CPU semaphore mapping and
eligible engine properties, it compares the recorded completed sequence
(`scheduler+0x137c+4*q`) with the deferred-cleanup sequence (`+0x1b3c+4*q`).
It passes the former as the cleanup limit. It does not use the submitted
sequence at `+0xf9c` to retire pending records in this tested path.

`e21e20` obtains a per-queue exclusion flag at `scheduler+0x4010+4*q`, with
original atomic operations surrounded by modeled OS queued spin locks. It
advances `+0x1b3c` before calling the selected record's `e21448`, then releases
the exclusion flag. A busy flag prevents cleanup. The helper itself trusts
its supplied limit: a direct test with the limit ahead of the completion
cache still processes the supplied records. It is therefore not a standalone
completion/quiescence predicate. Static callers `e1ab38` and `e1ce00` supply
the submitted sequence under other surrounding policies; those larger paths
are not qualified here.

The selected-record body requires modeled IRQL 2, unlike the earlier
notification's interrupt-level context. For `job.flags & 0x18 == 0`, it:

1. Invokes optional `job+0x118` only for object classes `(1,1)` or `(4,1)`;
   class `(5,2)` skips that branch.
2. If `job+0x130` is nonzero, calls the original import trampoline for
   `KeInsertQueueApc(job+0x150, job[0x130], job[0x138], 0)`.
3. Updates `scheduler+0x2abc+4*q` with the OS fence at `job+0x30`.

The tested path clears a nonzero `job+0xf8` link at the end, even with the
suppression flags set. Byte-for-byte checks show that the rest of each record
remains present and unchanged, including its embedded APC and callback data.
No DMA allocation is freed or unmapped along this executed path. Nonzero
`job+0x148`, graphics-specific fields, scheduling events and other optional
branches are not included in that claim.

The APC is a callback dispatcher, not inherently a free operation. Static
initialization at `19c47ac` passes `e063c0` as the kernel APC routine for each
ring record's embedded `+0x150` APC. The test separately executes that original
routine with the queued arguments: it subtracts `0x150` to recover the job,
then invokes the function supplied as APC argument 1 with `(job, argument2)`.
That final per-job callback remains synthetic; neither Windows APC scheduling
nor the full ring initialization executes. Static producer `dd28f8` copies
the callback and context from private-record `+0x178/+0x180` to job
`+0x130/+0x138`. The concrete runtime-generated path is now traced below and
leaves these fields zero; the synthetic APC cases must not be mistaken for
proof that DLA uses this optional mechanism.

There are 195 passing cases: four sequence boundaries, zero/one/three completed
records, two queues, two object classes and four suppression-flag combinations,
plus busy and direct-helper controls. Every case has an extra submitted record
which must remain untouched. The multi-record cases also cross ring-slot and
32-bit sequence boundaries. Actual imports for IRQL, queued spin locks and
APC insertion are verified against the pinned PE before testing. These cases
now use job type 1, as produced by the virtual-submit translator below;
their nonzero optional callbacks are still deliberate test inputs.

This establishes a separate deferred callback stage and rules out treating
the cleanup counter or cleared job link alone as proof of physical DMA
reclamation. The native retained-memory policy remains unchanged. The concrete
private-record producer below rules out the APC as a demonstrated DLA
destruction mechanism on that path.

```sh
venv/bin/python scripts/inspect_kmd_cleanup.py > results/kmd-cleanup.json
```

## Runtime private data to concrete NPU job

`scripts/inspect_kmd_virtual_submit.py` connects the original runtime-generated
private block to the original KMD translation and job producer. It first runs
the existing `nvdla_runtime.dll -> nvcudla.dll -> D3DKMTSubmitCommand` harness,
then transfers its actual 852-byte private block into a constructed kernel
submission argument. It executes:

```text
19b1500 -> dca010   original object-class (5,2) dispatcher
dd3130             validate/translate runtime private header
dd6188             populate expanded kernel private metadata
dd28f8             allocate ring slot and populate job record
[final virtual dispatch modeled; no firmware submission]
```

The argument layout matches Microsoft's 64-bit
[DXGKARG_SUBMITCOMMANDVIRTUAL](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkarg_submitcommandvirtual):
context at `+0`, command GPU address at `+8`, command length at `+0x10`, private
pointer at `+0x18`, allocated private capacity at `+0x20`, UMD data length at
`+0x24`, OS fence at `+0x28`. The two private lengths are distinct: the OS
allocates the KMD-requested capacity and copies in the UMD bytes. The test
constructs capacity `0x590`; it does not execute CreateContext negotiation,
public DDI registration or the actual OS scheduling transition.

The original translator recognizes the runtime's `ADVN`, version 4/subtype 1,
zero flags and `0x354` header. It clears a `0x590`-byte temporary structure,
sets type 1, populates adapter/device references and sequence bookkeeping,
then invokes `dd6188`. With the runtime's zero payload, optional imported
handles, callbacks and auxiliary data remain absent. The expanded structure
is copied into the kernel private buffer; marker byte `+0x53c` becomes 1 and
DWORD `+0x540` becomes `0x4e564441`. The memset/memcpy functions are exact byte
models, while header validation, expansion, field copies and branches execute
as original instructions.

The resulting job has type 1, its own queue sequence at `+0x2c`, the independent
OS fence at `+0x30`, the submitted command GPU address at `+0x38` and the expanded
private pointer at `+0xf8`. **Every byte in `job+0x108..0x1af` is zero.** In
particular, the interrupt callback, deferred callback, APC callback/context,
registry handles and embedded APC are absent. This corrects the earlier
hypothesis that the generic cleanup APC necessarily provides the DLA buffer
destruction step. It does not establish that every possible runtime submission
variant has the same behavior.

The 32 cases cover four sequence boundaries, two OS fences, two queues and
with/without a second invocation using the translated metadata. That second
invocation takes the original cached-metadata branch: it leaves the private
block unchanged and skips rebuilding it, but creates a fresh ring job with
the next internal sequence and the supplied new OS fence. The metadata cache
is not command deduplication or cancellation; this must not become automatic
retry behavior in the Linux runtime.

All object getters, OS locks, the runtime command builder and final KMD task
dispatch remain explicit modeled boundaries. The source blob is genuinely
produced by the pinned runtime, but no Windows process, device or physical
DMA is exercised. The remaining lifetime question is the device/firmware
retirement guarantee paired with WDDM-managed allocation lifetime, rather
than an assumed per-job APC free callback. Physical native-API repetition and
safe successful/error-path reclamation are still unqualified.

```sh
venv/bin/python scripts/inspect_kmd_virtual_submit.py > results/kmd-virtual-submit.json
```

## Complete version-3 descriptor and original runtime command consumer

Research date: 2026-10-28. `scripts/inspect_kmd_command_descriptor.py` now runs
the complete original KMD builder at `0xdd7460`. It uses the actual vtable at
`0xe9cb68`, its size getter `0xdd73b0`, ring-address helpers, pre/postaction
builders and context-header initializer `0xdd73f0`. The engine-property getter,
context memset and logging remain modeled, with constructed job, mapping and
fence-provider objects. The optional profiling gate is disabled.

```sh
venv/bin/python scripts/inspect_kmd_command_descriptor.py > results/windows-kmd-command-descriptor.json
```

The test has 32 cases: 21 KMD descriptor cases, eight engine-dispatch/IRQ cases
through the original runtime builder and firmware, and three UMD bypass cases.
It requires the pinned vendor DLL/KMD/firmware and generated ReLU artifacts.
No hardware access or Linux driver change is involved.

### The KMD wraps the UMD command list

The inspected builder does not decode or rewrite individual user commands.
For a nonzero command byte length divisible by 64, it reads the device pointer
from `job[0xf8]->u64[0x38]` and uses the low 16 bits of `length >> 6` as the
command count. The job's metadata/mapping object is constructed in this test;
the OS GPU-VA mapping lifecycle that supplies that pointer is not executed.
There is no dereference of the UMD command address in the KMD builder test.

The emitted outer descriptor uses version **3**, engine `0x44`, and queue 0.
Its size getter returns `0x500`, computed from two `0x1a1` action capacities
plus overhead and rounded to a 256-byte boundary. Descriptor slots use
`sequence & 31`.

| Descriptor offset | Size | Observed value |
| --- | --- | --- |
| `0x00` | u64 | Next pointer zero |
| `0x08`, `0x09` | u8 each | Version 3, engine `0x44` |
| `0x0a` | u16 | Descriptor size / ring stride `0x500` |
| `0x0c` | u16 | Low 16 bits of internal sequence |
| `0x0e`, `0x0f` | u8 each | Pre/postaction-list counts |
| `0x10`, `0x12` | u16 each | Pre/postaction table offsets 60 and 64 |
| `0x14` | u8 | Queue zero |
| `0x15` | u64 | UMD command-list device address |
| `0x1d` | u16 | UMD command count |
| `0x21` | u64 | Literal budget value 1800; time units not calibrated here |
| `0x29` | u16 | Bit 2 set; bit 1 copies job flag bit 7; bit 0 set for missing/invalid commands |
| `0x2b` | u64 | Zero with the profiling gate disabled |
| `0x33` | u64 | Per-slot context-header device address |

Missing metadata or zero/misaligned command length produces zero pointer/count
and sets flag bit 0; misaligned nonzero length also logs a diagnostic. This
function still returns zero. A byte length of `0x400000` is divisible by 64
but truncates to count zero without setting that flag. These are observed
vendor behaviors, not validation rules to copy into Linux.

The original context initializer writes a separate 64-byte header. Byte 0 is
1; for a constructed context of type 2, byte 1 copies context `+0x2b0` and u64
at byte 4 copies context `+0x2c8`. Otherwise the remaining bytes are zero. The
header device address is `ring_base + 32*stride + (sequence & 31)*64`.

The subsequent [original-firmware checkpoint test](contract.md#original-firmware-checkpoint-save-and-restore)
establishes that the header's +4 pointer addresses a separate 32-byte scheduler
checkpoint. It also exercises the restore gate: job flag bit 7 becomes task
flag bit 1, which permits consuming a checkpoint with its saved-work indicator
set. This is a consumer test using a constructed context object, not a trace of
the Windows context allocator or a physical context-switch qualification.

Preactions occupy descriptor offset 68 and encode the previously recovered
prior-completion wait, entry marker and control gate. Postactions start at
offset `0x1e5` and write the current sequence to the completion semaphore.
Full-byte comparisons verify these actions together with the outer header,
including wraparound sequences, disabled preactions, job flags and context data.

### Original firmware consumes both descriptor layers

The integrated cases use original `nvdla_runtime.dll` builder `0x743a8` to
produce the inner UMD descriptor, address list and inline preaction, then use
the original KMD builder to produce the outer version-3 task. Their allocation
addresses are projected into one RAM-backed DMA space containing the pinned
compiler-produced ReLU model. This explicit glue models allocation and address
mapping; it is not a live D3DKMT-to-kernel call.

Unmodified HAL-1 firmware runs from reset through trap submission, scheduler
and engine IRQ handling using the existing register-level DMA model. With
the UMD bypass flag clear it:

1. Accepts the outer task and executes its preactions.
2. Reads the version-2, 64-byte UMD command and executes its inline preaction.
3. Reads the model's address list/network/instructions and programs all seven
   expected engine-address pairs.
4. Remains active until the modeled engine status reports completion.
5. On an injected engine IRQ with status 1 or 5, writes the outer completion
   sequence, retires the task and asserts SWGEN0. Status 0 or 4 leaves it active.

Both builder flag arguments 0 and 256 exercise this arithmetic-dispatch path,
because only the low byte controls the emitted UMD flag. All eight cases keep
input unchanged and output poisoned: no arithmetic engine is emulated and no
numerical result is claimed.

### UMD bit 0 bypasses arithmetic but still completes

Firmware PC `0x101938` reads UMD byte `+0x1f`; if bit 0 is set, it branches to
`0x101a9e` instead of entering the arithmetic dispatch at `0x101a9c`. Original
builder arguments 1, 7 and 255 all set that bit. The three integrated cases
verify that the UMD preaction runs and the outer completion semaphore and
SWGEN0 are produced, while no engine-address programming occurs. Output stays
poisoned. Thus even a fully completed version-3 task can deliberately perform
no inference.

The earlier transport tests used a synthetic flag argument 7 to exercise
submission semantics. They are valid transport/bypass tests and must not be
reinterpreted as arithmetic evidence. The physical Linux ReLU evidence uses
the separate native version-2 path with its execution-bypass flag clear.

For Linux, retain the qualified version-2 path while using this version-3
contract as evidence for later per-command/multi-command support. Any new
arithmetic producer must explicitly clear the inner bypass flag, validate
counts before narrowing, and distinguish command completion from numerical
correctness. The successful RAM replay does not establish cache coherence,
engine DMA quiescence, safe allocation release or physical repeated inference.

## Software retirement is not device completion

Research date: 2026-10-28. The DLA adapter vtable at `0xe9c910`, slot `0x40`,
points to `0xdd52f0`. Static inspection shows a capability/state gate at
`0x1a89a68`, the previously recovered control-gate writer `0xe18220`, and an
OS synchronization boundary `0xdca2b0`. The latter selects helper `0xe183b0`
or `0xe18a30` according to capability bit 13. The caller subsequently performs
additional context, entry-bank and queue operations before reopening the gate.
This is not a recovered complete physical-reset procedure.

`scripts/inspect_kmd_recovery.py` isolates and executes original `0xe183b0`
with the capability-bit-clear selection's constructed queue/context state.
It uses the original entry-bank accessors `0xdd1548`, `0xdd1428`, `0xdd7708`,
the concrete adapter predicates `0x5bf0`/`0x5be0`, and worker wrapper
`0xdd6440`. The context callback now executes its original implementation
`0x92c30`, resolved below. The descriptor callback now also executes original
`0xdd6dd0`; IRQL, timestamps and logging remain modeled. The original helper
requires IRQL above 2; the
test supplies 3, rather than claiming an actual Windows scheduler context.

For outstanding records with nonzero submission timestamps, job type 1 and
the tested context mode `+0x8c = 2`, the helper:

1. Reads the entry-marker bank once, without polling the completion bank.
2. Sets job flag bit 3 and appends each outstanding internal sequence to the
   caller's output array.
3. Calls the first selected context's virtual slot `0x98` with argument zero,
   sets context `+0x234` bit 2 and records the selected job. Later selected
   records go through the worker callback. The context callback increments
   only context `+0x6c`; the worker rewrites the queued descriptor as described
   below, without waiting for hardware completion.
4. Advances software `scheduler+0x137c+4*q` and `+0x1b3c+4*q` to the submitted
   sequence, clears the outstanding counter at `+0xbb8+4*q`, clears the byte
   at `+0x3754+q`, and copies `+0x384c+4*q` to `+0x3c2c+4*q`.
5. Returns boolean true and status zero, even though the modeled device
   completion word still contains the earlier sequence.

Here `q = node*8+engine`. The reported OS fence normally comes from the first
selected record's `+0x30`. If job flag bit 2 is set and bit 9 clear, the helper
retains the prior reported fence instead. An empty queue performs bookkeeping
without reading either semaphore bank. For the tested first-record context
mode zero, the helper takes an earlier rejection path with status
`0xc00000bb`, without advancing the completion/cleanup counters.

Sixteen cases cover zero, one and three outstanding records, ring wrap, u32
sequence wrap with a valid current entry marker, OS-fence flag combinations,
and that rejection. Read/write hooks verify that no semaphore-bank write is
performed and that the completion bank is not read; the return/output counters
are checked against the original code's writes.

Replacing the worker stub exposed a previously hidden diagnostic in the
`previous=0x7fffffff, count=3` fixture: the original callback compares a u16
descriptor sequence with a full u32 job sequence. That case now explicitly
records two vendor assertions and models their return before continuing.
It does not establish that such a high sequence is a valid live reset state.
The remaining cases require no returning vendor assertion.

```sh
venv/bin/python scripts/inspect_kmd_recovery.py > results/windows-kmd-recovery.json
```

The result does not show that Windows frees live DMA unsafely: the caller's
gate and synchronization, the alternative helper, descriptor-fetch races
and surrounding recovery still matter. It does show why copying software
counter advancement from this path into Linux cannot establish device
completion or authorize buffer reuse. The normal device-semaphore reader and
this software retirement helper have different evidence boundaries. Our Linux
fail-stop queue and retention policy remain unchanged; safe error reclamation
requires a demonstrated stop/reset contract, not merely matching counters.

## Concrete context and process accessors (2026-10-28)

`scripts/inspect_kmd_context_reset.py` executes two original creation prefixes
from the registered DDI wrappers. This establishes which concrete vtables
belong to the tested DLA class, rather than injecting callback identities.

```text
CreateDevice: DDI +0x90 -> 0x1ab1000 -> 0x198b500
  -> 0x19b0a10 -> class predicate 0xdca010 -> 0x19d9700
  -> factory 0x1a40510 -> constructor 0x1a3ff08
  -> base 0x1a24540 -> 0xdc24b8
  -> device vtable 0xea0f08 published; stop at 0x1a3ff40

CreateContext: DDI +0x1d0 -> 0x1ab0fd0 -> 0x198b190
  -> device virtual +0x28: 0x1a40360 -> factory 0x19e0ce0
  -> base 0x1a26038 -> 0x1a24540 -> 0xdc24b8
  -> embedded state 0x19a9d10
  -> context vtable 0xe9c628 published; stop at 0x19e0d84
```

The device constructor copies creation-request `+0x18` into device `+0x98`,
and the adapter pointer into device `+0x90`. Microsoft's
[DXGKARG_CREATEDEVICE layout](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkarg_createdevice)
identifies the former ARM64 field as `hKmdProcess`, a KMD process-object handle.
Device vtable slot `+0xa0` is `0x8c7e0`: precisely `ldr x0,[x0,#0x98]; ret`.
The test executes it with three distinct handles, including null, and checks
that the device object is unchanged. Thus the accessor used by the reset
helper returns process state; it does not wait for hardware or stop DMA.

The context factory installs type 2 at `+0x70`, device at `+0x78`, node/engine/
mask at `+0x80/+0x84/+0x88`, and initial mode 11 at `+0x8c`. Mode 2 used by
the recovery fixture is selected explicitly with original setter `0x1a2a470`;
it is not asserted to be the live default. Context vtable slot `+0x98` is
`0x92c30`, an exclusive-load/store loop incrementing the u32 at `+0x6c`,
followed by a barrier and return. It contains no hardware call.

Three node/engine combinations and four initial counter values each cover
zero, ordinary increments, the signed boundary and u32 wrap. Whole-context
byte comparisons and write hooks check the sole counter update. The existing
16 recovery cases now execute this callback and verify its counter result.
All 29 reset/reload cases use the original process accessor where applicable,
including the null-handle rejection, instead of a synthetic virtual return.

These are deliberately constructor **prefixes**, not completed Windows DDI
creation. Pool allocation, OS primitive initialization, memset, timestamps
and logging remain modeled at documented boundaries. No physical reset,
live Windows process or DMA quiescence is established. The surrounding
scheduler/reset sequence must still be traced. The worker callback
is resolved in the following section.

```sh
venv/bin/python scripts/inspect_kmd_context_reset.py > results/windows-kmd-context-reset.json
```

## Recovery rewrites a queued descriptor; it does not drain DMA

`scripts/inspect_kmd_recovery_descriptor.py` executes scheduler initialization
`0x19e1580` through factory `0x19e28b0`, the original constructors and ring
initializer `0x19e2750`. Adapter node getters are original; allocation, address
translation and memset are modeled. With the constructed single node of type
`0x33`, the selector at adapter `+0x4648` chooses:

| Selector | Constructor | Vtable | Ring allocation |
| --- | --- | --- | --- |
| `0xe0000023` | `0x19e2a00` | `0xe9cb00` | `0xa000` bytes |
| `0xe0000025` / `0xe0000026` | `0x19e2a40` | `0xe9cb68` | `0xd000` bytes |

These are fixture selector values, not newly identified physical chip IDs.
All three cases store the manager at scheduler `+0x8680`, use a descriptor
stride of `0x500`, and install virtual slot `+0x18 = 0xdd6dd0`. Consequently
the already traced `0xdd6440` invokes a descriptor-rewrite callback, not a
generic worker with unknown implementation.

Original `0xdd6638` finds the host ring slot using `sequence & 31`. After the
sequence comparison, `0xdd6dd0` performs only these descriptor changes:

| Offset | Original operation |
| --- | --- |
| `0x1d` | Clear u16 command count |
| `0x29` | Set bit 0 in u16 flags, preserving other bits |
| `0x15` | Replace u64 command-list pointer with `0xdeadf000` |

It then issues `dmb st` and returns success. There is no completion-bank read,
poll, MMIO access or engine-stop call in this callback. Tests compare the
complete ring before/after for five sequences on each constructor variant,
including ring wrap and `0xffff`, and repeat every rewrite to check idempotence.
Two negative cases stop at the original assertion before any rewrite: a
mismatching sequence and a u32 sequence `0x10000` whose descriptor stores only
its low 16 bits. The integrated 16-case recovery test now executes the concrete
callback, checks its ring writes and records the high-sequence diagnostic.

The original HAL-1 RISC-V firmware consumes an actual KMD-built v3 descriptor
after the original Windows callback has rewritten it. With RAM-only DMA/MMIO:

- **Rewrite before submission:** the firmware executes the outer actions,
  writes entry/completion words, emits one task interrupt and retires the queue.
  It neither requests the command-list pointer nor dispatches arithmetic.
- **Rewrite after the execution-header DMA:** a hook at `0x1016b6` first checks
  that firmware-local memory contains the original 60-byte header, then changes
  only host backing to the rewritten descriptor. The firmware still reaches
  original DMA helper `0x104ed8` requesting the **old command-list address**.
  The test stops there, before performing that DMA or executing any command;
  no completion or task interrupt has occurred.

The firmware code remains byte-identical in both tests. The late rewrite is
an explicit modeled host interleaving, not an observed physical race. It proves
why descriptor rewriting alone cannot supply cancellation/reclamation semantics:
already fetched state can survive the update. The surrounding Windows reset
ordering may address that race; it has not yet been fully reconstructed.
Do not add a native cancel ioctl or release DMA based on this callback alone.

```sh
venv/bin/python scripts/inspect_kmd_recovery_descriptor.py > results/windows-kmd-recovery-descriptor.json
```

## Outer reset ordering and software-produced completion

`scripts/inspect_kmd_reset_order.py` executes the complete outer body
`0xdd52f0`, including its original capability check `0x1a89a68`, control-gate
writer `0xe18220`, recovery helper `0xe183b0`, concrete context/descriptor
callbacks, process getters, bookkeeping and deferred-record producer `0xe17420`.
The constructed adapter has capability bit 28 set, capability bit 13 clear,
node 2/engine 1, and no optional history object at scheduler `+0x8678`.
The original history helper `0x1a894f0` therefore returns true immediately;
it is not a hardware-idle wait on this path.

OS spinlocks, IRQL, events and clock calls are modeled using checked PE import
identities. The `0xdca2b0` synchronization boundary calls the original recovery
helper under modeled IRQL 3 and propagates its boolean result, then reports
successful OS synchronization. The reset/reload helper `0x19e2628` is a
recording success/failure boundary here; its real RM/HFRP chain is tested
separately below. Thus this test closes outer ordering, not physical reset.

For three outstanding jobs, the successful outer path does the following:

1. Writes `0xf00d` into the firmware-visible control semaphore and signals
   the queue's two OS events.
2. Executes recovery under OS synchronization: advances software counters,
   updates the selected context and rewrites later task descriptors.
3. Releases the node lock, checks the optional history policy, and invokes
   reset/reload for the selected type-2 context.
4. Queues three 56-byte deferred records at scheduler `+0x7f08`. Each contains
   the completion-word pointer, a prior sequence (`0`, `1`, `2`), the entry-word
   pointer, and zero mode/auxiliary fields.
5. Writes latest sequence `3` into the entry semaphore, records timing,
   writes `0xfeed` into the control semaphore, signals both events again,
   and returns success. The completion semaphore remains zero at this point.

On a modeled reset/reload failure, step 2 has already advanced software
counters. The caller skips deferred-record creation and the final entry write,
reopens the control gate, signals the events, and returns `0xc0000001`.
A rejected context mode returns `0xc00000bb` after closing/reopening the gate;
a missing capability rejects before touching the gate. The empty-queue path
can return success without calling reset/reload. These return values and
software writes are not interchangeable with engine quiescence.

### The deferred consumer writes completion itself

The same test passes the **actual records produced above**, with their original
semaphore pointers, into one iteration of original consumer `0xe16cf0`.
The queue's adapter pointer and OS wakeup are modeled; the iteration stops at
`0xe17194`, after releasing its lock and before the subsequent synchronized
notification. Thread creation and that notification are not executed.
The original consumer uses the same queue stride, count and event offsets
as the producer/gate helpers. Its type-0 record behavior is:

- A completion value behind the expected prior sequence, using signed u32
  subtraction, leaves that record pending.
- An equal completion value causes a host write of `expected + 1` to the entry
  word at `0xe16e44`, then to the completion word at `0xe16ed0`.
- A value ahead of the expected sequence retires the record without either
  write. The test's memmove/memset boundaries only compact/clear record storage.

Four cases start with completion `0`, `1`, `3` or `0xffffffff`. The first two
advance completion to 3 through original **CPU stores**, the third retires all
records without semaphore writes, and the last leaves all three pending with
completion unchanged. No firmware or arithmetic-engine execution is involved.
The first case therefore demonstrates that recovery can synthesize all three
completion advances without a new hardware completion.

The suite has twelve cases: six empty/nonempty reset outcomes, capability and
context rejection, and four deferred-consumer inputs. Memory-write hooks assert
exact gate/entry/completion addresses, values and originating instruction RVAs.
The Linux normal-completion contract must remain distinct from this Windows
error-recovery bookkeeping. A matching semaphore after reset cannot justify
DMA release without an independently established reset/drain guarantee.
The next hardware-contract investigation remains the reset/reload boundary,
including HFRP/controller semantics and the loader's idle/reinitialization path.

```sh
venv/bin/python scripts/inspect_kmd_reset_order.py > results/windows-kmd-reset-order.json
```

## Identifying the Windows reset entry points

Research date: 2026-10-28. `scripts/inspect_kmd_reset_dispatch.py` executes
the original global callback builder `0x6e96718` and ordinary public DDI table
builder `0x6e95680`. It then executes each wrapper up to its implementation
boundary, checking argument and return-value preservation. The field names
come from Microsoft's [DRIVER_INITIALIZATION_DATA layout](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/dispmprt/ns-dispmprt-_driver_initialization_data),
using ARM64 pointer alignment and retaining reserved entries.

| DDI | Table offset | Wrapper RVA | Implementation RVA |
| --- | --- | --- | --- |
| StopDevice | `0x18` | `0x1ab0d30` | `0x1aa1e90` |
| ResetDevice | `0x68` | `0xe312f0` | `0xaa4c0` |
| ResetFromTimeout | `0x100` | `0x1ab1700` | `0x19944e0` |
| RestartFromTimeout | `0x108` | `0x1ab1730` | `0x19946f0` |
| ResetEngine | `0x250` | `0x1ab16f0` | `0x1994260` |
| SubmitCommandVirtual | `0x2d0` | `0x1ab19d0` | `0xdb57e0` |
| ResetHwEngine | `0x3a0` | `0x1ab1b60` | `0x199a600` |
| ResumeHwEngine | `0x478` | `0x1ab1bc0` | `0x199aac0` |

Most wrappers dispatch through globals populated by the first builder;
ResetEngine and SubmitCommandVirtual use direct branch thunks. This test does
not execute DriverEntry, OS registration, or the conditional alternate table
initializer `0x6e97120`. It identifies the ordinary table, not which Windows
installation policy selects it at runtime.

Four additional cases execute the original DDI wrappers and routing bodies
with a class `(5,2)` adapter, stopping before the reset implementation:

- **ResetEngine:** `0x1994260 -> 0x19b1100 -> adapter vtable +0x378 ->
  scheduler vtable +0x40 -> 0xdd52f0`. The class test at `0xdca010` runs
  unchanged, as does the concrete accessor `0x91260`, which reads adapter
  `+0x52e0`. The adapter vtable is `0xe9c050`, and the concrete
  scheduler vtable is `0xe9c910`. Thus the preceding section's software
  retirement helper is conclusively inside a per-engine reset path.
- **ResetFromTimeout:** with adapter byte `+0x480c = 1`,
  `0x19944e0 -> 0x19b11c8 -> 0x19d9f80`. This is a different DLA-specific
  implementation, not another entry into `0xdd52f0`.

Both routes preserve injected success and `0xc00000bb` failure from their
stopping boundary. Logging and timestamps are modeled; original stack-cookie
helpers execute. The twelve cases establish callback identity and routing,
not the semantics or success of any physical reset.

Static inspection of the timeout body `0x19d9f80` supplies the next concrete
investigation targets. It visits context lists, calls `0xe23c80(adapter, 2)`,
increments adapter `+0xf10`, processes scheduler `+0x7f08` via `0xe16c78`,
calls scheduler virtual slot `+0xc0` and adapter slot `+0x118`, and later
calls `0x19df220`. That latter routine calls `0x19e3b80` on adapter `+0xed8`,
invokes its virtual destructor, clears the pointer, and calls `0x19d7eb8`.
Subsequent scheduler `+0x90` and adapter `+0x388` calls precede
`0xe23c80(adapter, 3)`. These are observed call relationships; none is yet
labeled as a proven hardware-stop or DMA-drain operation.

```sh
venv/bin/python scripts/inspect_kmd_reset_dispatch.py > results/windows-kmd-reset-dispatch.json
```

The Linux driver must still retain submitted DMA until the relevant physical
stop/completion boundary is established. A Windows callback named ResetEngine
and a software success code do not themselves provide that guarantee.

### Timeout resource teardown and RM state control

The class `(5,2)` constructor fragment at `0x19dc498` passes those class
values to base initialization at `0x19d2f88`, then installs vtable `0xe9c050`
at `0x19dc4b8`. This resolves timeout-path adapter slots `+0x118` to
`0xdd3800`, `+0x378` to `0x91260`, and `+0x388` to `0x19dc1a0`.
`scripts/inspect_kmd_reset_resources.py` executes the first resource path
and the separate semaphore teardown. The initial fourteen boundary cases are
now joined by eighteen concrete-constructor interrupt-mask cases below.

`0xdd3800` tests adapter `+0xc34` bit 3. If set, it calls `0xdd1f08` with
the u32 at adapter `+0x46b8`, then clears bit 3. The original route continues
through `0xd36cb8`, the RM-root accessors `0x93630`/`0xd39160`, and the
identity check `0xd3b198`. A successful check requires the requested value
to match `*(*(context+0x150))+0x58`. It then calls `0xd39a60(context, 0)`:

- An optional bridge passes through `0xd3e340 -> 0xd4bbd0`, which writes a
  software byte at bridge target `+0x40d8`.
- For a non-null `context+0x178` object, the code writes its `+0x50` word
  and invokes virtual slot `+0x220` with `(context, object, 0)`.
- That virtual callback is an injected boundary in the initial cases. The
  concrete cases below resolve it to interrupt masking. Its return register
  is not used by the surrounding routine.

The tests execute the original lookup and state-write code with the bridge
present and both present/absent RM-device objects. Missing RM root/context/lookup object,
missing identity pointers, and identity mismatch make the lookup return zero.
If the vendor assertion handler returns (as explicitly modeled), the caller
still clears adapter bit 3. An already-clear bit skips RM lookup entirely.
A missing `context+0x178` object also skips the virtual callback while the
outer lookup returns success. Thus neither the adapter bit nor that success
return establishes hardware quiescence. This is not a claim about what a
non-returning Windows assertion handler would do.

The second path is `0x19e3b80 -> vtable 0xe9cc00 slot 0x18 -> 0x19e38a0`.
This vtable is installed by the class `(5,2)` selection's semaphore constructor
`0x19e2aa0`, called from `0x19e3790`. With an allocated semaphore bank, original
`0x19e38a0` invokes `0xdc8158 -> 0xdc5070(manager, adapter, allocation, 0, 6)`.
The test stops at `0xdc5070`; the allocation manager's release semantics are
not modeled. It then observes the original pointer/size clearing, original
virtual destructor `0x93100`, and pool-free boundary `0xe0c8d0`. Missing owner
or allocation cases skip the corresponding calls. No hardware-stop operation
appears in this isolated prefix before the allocation-manager boundary.

Logging, timestamp reads, a returning assertion handler and allocator-release
boundaries are synthetic. The initial cases also inject the RM virtual
callback, while the concrete cases below execute its body with modeled
register I/O. These tests do not execute the full timeout reset or a physical
device. Replacing Linux retention with the observed pointer clearing would be
unjustified.

```sh
venv/bin/python scripts/inspect_kmd_reset_resources.py > results/windows-kmd-reset-resources.json
```

### The resolved RM callback masks interrupts, not engine DMA

The context initializer at `0xd39400` (selection visible at `0xd395a4`)
creates one of two RM objects. For selector `0x71` it calls `0x195d038` and
stores the result at context `+0x170`; for `0x72` it calls `0x1964a48` and
stores it at `+0x168`. Both paths take the constructed object's `+0x68`
subobject pointer and publish it at context `+0x178` (`0xd39690`). The
constructed subobject is at allocation `+8`, not an independent allocation.

The expanded test executes both complete original constructors with
caller-provided storage and parent registration disabled (constructor flags
3). It uses the initializer's `0x354` branch values: `0x404044` for the first
constructor and `0x50200` for the second. Original base constructors, selector
decoders and HAL-function initializers execute. Full context initialization
and the live Windows choice between these object selectors are not tested.

| RM object selector | Subobject vtable | Slot `+0x220` thunk | HAL field | Selected implementation |
| --- | --- | --- | --- | --- |
| `0x71` | `0x108e9b8` | `0xd3c210` | allocation `+0xd8` | `0xd49800` |
| `0x72` | `0x108ef08` | `0x641bc0` | allocation `+0x298` | `0xd4d9f0` |

The test then enters original adapter callback `0xdd3800` and follows the
entire RM lookup/state-control route into those concrete implementations,
without replacing the virtual callback or its forwarding thunks:

- Selector `0x71` reads logical register `0xd28`, intersects it with the
  subobject's saved ownership mask at `+0x54`, and writes the result to
  `0xd24`. This is the already independently recovered HAL-1 interrupt-mask
  policy in [the firmware contract](contract.md#hal-1-interrupt-mask-policy-after-method-2-idle).
  The generic accessor resource index is 0.
- Selector `0x72` reads `0xd28` and `0xd2c` through resource index 1,
  intersects the two values, and writes the result to `0xd24`.
- Both preserve their saved mask if the intersection is zero. A first-read
  failure skips the write. The surrounding RM route returns lookup success
  and the adapter clears its bit even when the modeled register read/write
  fails. This does not make an accessor failure a successful hardware action.

Three mask pairs and success/read-error/write-error scenarios cover each
constructor, adding eighteen cases (32 resource cases total). The second
read's independent failure behavior in the `0x72` implementation is not
covered. Actual register I/O is modeled at `0xd39878`/`0xd39f38`; the original
accessors determine the resource indices and offsets.

This closes the earlier unresolved virtual-call identity. Interrupt masking
suppresses notification; it is not an engine halt or proof that DMA has
finished. No engine-stop request is emitted by this specific callback path.
The remaining quiescence investigation belongs in the per-engine reset's
context operations and the surrounding timeout/allocation-manager contracts,
not in interpreting the adapter's cleared software bit as hardware completion.

## Engine-reset RM command and firmware reload (2026-10-28)

The per-engine reset body `0xdd52f0` contains an additional context branch
at `0xdd5654`. Static inspection shows that a selected context with `+0x70`
equal to 2 reaches `0x19e2628(scheduler, selected_job)` after the preceding
context callback, node unlock and `0x1a894f0` check. Its boolean failure is
converted to `0xc0000001`. This does not establish the full reset body's
effects. The job object's accessor is now resolved by the creation-prefix
tests above.

`scripts/inspect_kmd_reset_reload.py` executes original helper `0x19e2628`
through the following recovered route:

```text
selected_job+8 -> device virtual +0xa0 (0x8c7e0) -> process+0x198
  -> 0xdd1960 -> 0xdd1c10 -> generic RM dispatch [modeled]
  -> command 0x35800005 handler 0xd44e40
  -> primary HAL+0x210 -> 0xd48b30
  -> 0xd41740 -> HFRP reset sequence -> common loader [modeled]
```

The original device accessor returns its constructed process-handle field.
The test also covers a null handle, which makes the original helper return
false without an RM request. It executes the full original HAL constructor
`0x195d038` with flags 3 and selector `0x404044`, and verifies the installed
`+0x210` target. Thus the HAL virtual callback itself is not injected.

Original `0xdd1960` builds this RM packet, independently of its second entry
argument: object handle `0x35800000`, command `0x35800005`, flags zero, payload
length 2, payload bytes **`00 00`**, and initial status `0x56`. The client
handle comes from input `+8`; dispatch hints carry input `+0x10`. A nonzero
global gate at RVA `0x149ba3c` rejects the operation before dispatch. The
readonly record at `0x108e840` independently identifies handler `0xd44e40`
and payload size 2. The test validates that record, models generic dispatch
to its target, and returns the original handler's result in the RM status
field. It does not execute the generic RM object dispatcher.

The handler resolves the RM context through original `0x93630`, obtains its
primary HAL from `+0x170`, and forwards the request to `+0x210`. For an enabled
HAL (`+0x470 != 0`), original `0xd48b30` sets byte `+0x5ac`, runs diagnostic
reads, writes `0x80` to logical IRQMCLR `0xd24`, and invokes the recovered
pre-loader parent with mode byte 1 of the request: **mode 0**. It finally
writes `0x80` to IRQMSET `0xd20`, including when that parent returns an error.
Generic register access uses resource index 0 and is modeled.

With capability 1, the original parent/helper send:

| Order | HFRP command | Payload |
| --- | --- | --- |
| 1 | `0x19b` RESET_IDLE | `03` |
| 2 | `0x19a` RESET | `01` |
| 3 | `0x19b` RESET_IDLE | `0c` |
| 4 | Common loader `0xd439c0` | `(outer, outer+0x170)` |

The names follow the pinned NVIDIA R38 command definitions documented in
[HFRP prerequisites](hfrp-irq.md#pre-loader-reset-and-idle-commands). This
joins the previously isolated sequence to an engine-reset helper; it does
not establish the meaning of individual RESET_IDLE bits, that all Windows
resets choose this branch, or that cold startup requires these commands.

Seventeen cases check original entry traversal, exact packet/payload, event
ordering, the HAL software flag, and returned status. They include:

- A disabled HAL returns success without register writes, HFRP or loading.
- Capability 0 or 2 skips HFRP but still calls the common loader.
- A pending-work callback runs first when required; its error prevents HFRP
  and loading, but the surrounding IRQ re-enable still executes.
- Injected HFRP transport/response failures do not prevent loading and do
  not independently change the final boolean result.
- Injected IRQ-mask or IRQ-unmask write failures likewise do not change it.
- A common-loader error propagates through the RM status as boolean failure.

In those seventeen policy cases, HFRP transport, common loading, pending
callback, diagnostic register values and OS/logging/timing boundaries are
synthetic. The additional wire cases below execute original transport.
The complete ResetEngine
DDI and physical hardware are not exercised. In particular, a successful
Windows helper return cannot be used as a Linux DMA-retirement guarantee:
several tested successful paths do not establish that a reset happened.
The native retention policy remains necessary until reset/idle semantics
and physical memory reclamation are independently qualified.

The previously modeled pending callback is now resolved as original power
wakeup `0xd40060`; see [wakeup before reset](hfrp-irq.md#power-wakeup-prerequisite-before-engine-reset-2026-10-28).
Its register `0x80` probe checks firmware OS-version presence, not idle. A zero
value can cause an additional common-loader invocation **before** the HFRP
reset sequence, whereas selected power-query/read errors are ignored. The
new replay executes that callback and power policy while retaining the
common-loader and hardware-transport boundaries explicitly.

```sh
venv/bin/python scripts/inspect_kmd_reset_reload.py > results/windows-kmd-reset-reload.json
```

### Reset requests through the original HFRP transport

Twelve additional cases (29 total) remove the `0xd3de30` sender stub. The
same job-helper/RM/HAL chain now executes original locked sender `0xd3de30`,
synchronous exchange `0xd4b568`, producer `0xd4b3a0`, poll helper `0xd3d750`
and consumer `0xd4b7f0`. Original `0xd4aeb8` initializes a separate mode-0
transport context. OS mutexes and register access are modeled; a synthetic
peer consumes each request and immediately publishes its matching response.
The handshake and controller-side execution of the commands are not simulated
or claimed. The transport is supplied in an already-established empty state.

The original chain produces these five-byte packets (header plus payload):

| Sequence | Command | Packet bytes |
| --- | --- | --- |
| 1 | RESET_IDLE | `05 01 6c 06 03` |
| 2 | RESET | `05 02 68 06 01` |
| 3 | RESET_IDLE | `05 03 6c 06 0c` |

Each exchange consumes the matching response, clears its outstanding-sequence
bit, acknowledges the reply doorbell, and releases the wrapper's mutex. The
test checks the original wrapper's logged transport and firmware statuses,
not just the eventual reset-helper boolean. Both a ring starting at zero
and one starting at byte 113 exercise packet production/consumption, including
wraparound. Each is tested with firmware status zero and `0x123`, and with
empty, one-byte `00`, and one-byte `ff` response payloads.

All twelve cases reach the modeled common loader and return boolean true.
Transport returns zero and preserves the injected firmware status separately;
the upper reset policy ignores that status after logging it. The original
reset chain does not decode the tested response byte as an idle indication,
and it accepts an empty response too. Consequently, transport sequence
retirement is evidence that the response was received, not that DLA memory
accesses have ceased. Resolving physical RESET_IDLE guarantees requires the
controller-side command semantics or an independently verified hardware idle
condition; interpreting the response byte as an idle flag is unsupported.

Only transport-register memory belongs to the synthetic peer. No DLA engine,
DMA, controller reset, firmware authentication or actual device is modeled by
these cases. Existing bounded Linux HFRP code already keeps firmware status
separate from transport success; this experiment does not justify adding a
Linux memory-release action after an HFRP acknowledgment.

The subsequent [complete SROOT reinit handler](sroot-transport.md#complete-reinit-handler-and-its-success-boundary-2026-10-28)
has now also been executed against a synthetic mailbox. For the valid KMD
request it checks transport and firmware status/detail, then returns without
a DLA-engine idle or DMA-drain read. Its optional output is a cached context
word, and malformed oversized requests can return success without transmitting.
This provider boundary therefore supplies no additional physical retirement
guarantee beyond the enclosing KMD checks and the still-opaque secure-firmware
command semantics.
