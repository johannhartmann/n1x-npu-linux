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

The 150 passing cases comprise 48 selected-job notifications, 70 selector
cases, 16 additional class `(5,2)` cases and 16 linked producer/firmware/reader
cases. Class `(5,2)` is checked by the DLA dispatcher `dd5ab0`; the additional
cases verify that the shared completion behavior does not depend on the
constructed class `(1,1)` used to cover optional graphics-side branches.
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
`+0x130/+0x138`. Reconstructing their concrete DLA values is a remaining lead.

There are 195 passing cases: four sequence boundaries, zero/one/three completed
records, two queues, two object classes and four suppression-flag combinations,
plus busy and direct-helper controls. Every case has an extra submitted record
which must remain untouched. The multi-record cases also cross ring-slot and
32-bit sequence boundaries. Actual imports for IRQL, queued spin locks and
APC insertion are verified against the pinned PE before testing.

This establishes a separate deferred callback stage and rules out treating
the cleanup counter or cleared job link alone as proof of physical DMA
reclamation. The native retained-memory policy remains unchanged. The next
useful trace is the concrete private-record callback producer and any actual
memory-destruction/reset operations it reaches.

```sh
venv/bin/python scripts/inspect_kmd_cleanup.py > results/kmd-cleanup.json
```
