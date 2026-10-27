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
the final sender. These are constructed objects; identifying the producer of
the job record and its relationship to WDDM's scheduling fence remains open.

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
been qualified. Trace the producer of `job+0x2c` and the corresponding Windows
completion notification next to connect this task sequence to the OS lifetime
contract described in [windows-buffer-lifetime.md](windows-buffer-lifetime.md).

```sh
venv/bin/python scripts/inspect_kmd_task_submission.py > results/kmd-task-submission.json
```

The 58 cases comprise 16 direct submissions, two global gates, missing-primary
handling, two address-encoding probes, 12 higher-level ring submissions,
24 firmware-consumer cases and one 64-bit postaction-address case. All run
offline; no device, boot configuration or register is changed.
