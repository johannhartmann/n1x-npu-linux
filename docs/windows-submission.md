# Windows runtime submission and WDDM synchronization

Research date: 2026-10-27. Original ARM64 instructions execute offline in
`scripts/inspect_windows_submission.py`. No device is opened and no Linux
memory-retention policy is relaxed.

## Result

The inspected runtime path issues three distinct WDDM operations on the same
context: optional pre-fence waits, command submission, then post-fence signals.
It does not wait for those queued post-fence signals before returning success.
A post-signal error is returned **after** command submission has returned
success. This makes the submission return code alone unsuitable as a test for
whether backing memory can be freed or a command can be retried.

Microsoft describes the signal operation as inserting a signal into each
specified context stream; the objects become signaled when the contexts reach
that point. This is an OS scheduling contract, not evidence that our firmware
mailbox ACK or SWGEN0 implements that same boundary.
[Signal structure](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_signalsynchronizationobjectfromgpu2),
[context monitoring](https://learn.microsoft.com/en-us/windows-hardware/drivers/display/context-monitoring).

## Reproduction and boundaries

```sh
venv/bin/python scripts/inspect_windows_submission.py > results/windows-submission.json
```

The test pins NVIDIA 617.42 inputs by SHA-256:

- `nvdla_runtime.dll`: `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b`
- `nvcudla.dll`: `8da1b42d35aa79c71e3102f95dde30a75dbe498983d386b3cb68082f6b467ba0`

Both images run in one Unicorn address space. Runtime keeps its preferred
`0x180000000` base; cuDLA is PE-relocated to `0x190000000`. The table below uses
RVAs, independent of that relocation. The original cuDLA dynamic resolver
executes and its API-name/global-pointer assignments are checked.

| Component | RVA or slot |
| --- | --- |
| Runtime submission | `0x75858` |
| Runtime device-map lookup | `0x2bfb0` |
| Runtime post-fence construction | `0x745a0` |
| cuDLA KMI table | `0xc2078` |
| Wait / submit / signal KMI slots | `0x80` / `0xc0` / `0x88` |
| Original KMI envelope builders | `0x97c0` / `0x9f78` / `0x9938` |
| Resolved API globals, respectively | `0xcfac0` / `0xcfb30` / `0xcfa58` |

The device-map tree is constructed, but its lookup code runs unmodified.
The device KMI getter, sync-object methods, command builder `0x743a8`, Windows
locks, CFG validation, loader APIs and D3DKMT calls are modeled. In particular,
the command GPU address/length are supplied by the modeled builder; this test
does not validate command payload generation. Constructed objects do not prove
which backend a live Windows machine selects.

## Recovered contract

Runtime submission requires one task. Context `+0x630` selects one of 32 slots
at `context + (index + 1) * 0x30`. The slot's `+0x28` sync object receives a
slot-`0x08` call, then a value getter (`0x18`) and setter (`0x48`) incrementing
the value. The test does not assign a wait/reset meaning to the unexecuted
slot-`0x08` implementation.

Pre-fence count is the task byte at `+8`; records start at `+0x18` with an
80-byte stride. Type byte 4 is required; subtype zero is included in the WDDM
wait, while other subtypes are skipped by this stage. Handle DWORD `+0x28`
and value DWORD `+0x48` become handle and zero-extended 64-bit value arrays.
The call is `D3DKMTWaitForSynchronizationObjectFromGpu`.

`D3DKMTSubmitCommand` receives the builder's GPU address/length, one context,
and a separate 852-byte (`0x354`) private-data buffer. Runtime overwrites its
header and clears the remaining 836 bytes:

```text
41 44 56 4e 04 00 01 00 00 00 00 00 54 03 00 00
```

The private data is not the executable command buffer; the Windows API exposes
them as separate fields. Do not treat the `0x354` byte length as a chip ID.
[Submit structure](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_submitcommand).

Post-fence count is task byte `+9`, with 80-byte records starting at `+0xa18`.
This path requires type 4 and subtype 1 and uses the same handle/value offsets.
It appends the runtime sync object's handle and updated value, then calls
`D3DKMTSignalSynchronizationObjectFromGpu2` with flags zero and one context.
The slot index advances modulo 32 only after that call succeeds.

The 14 cases check ordering, no external fences, 31 pre/post fences plus the
internal post-fence, zero extension of high-bit values, slot wrap, skipped
pre-fence subtypes and failures at validation, initial sync method, wait,
builder, submit and post-signal stages. Post-fence subtype validation and
sync-descriptor retrieval can also fail after a successful submit boundary.
These cases do not establish an upstream bound check for larger fence counts.

On the later error paths, the original function calls the sync getter/setter
again with a decremented value and leaves the slot index unchanged. This is
not a demonstrated command cancellation: the sync methods themselves are
modeled. Linux must preserve uncertainty after a possible submission and must
not infer device idleness from such a counter adjustment.

## Consequences for the Linux driver

Keep submission acceptance, output readiness, asynchronous error status and
resource retirement distinct. The existing executor's unique fence values,
fail-stop behavior and no-automatic-retry runtime remain appropriate. This
evidence does not justify freeing the retained DMA allocations.

Together with the [Windows buffer-release trace](windows-buffer-lifetime.md),
this locates the missing guarantee below the user-mode API: identify how the
KMD marks the command complete to the Windows scheduler, and prove the
equivalent firmware/engine boundary on Linux. Completion of a WDDM signal
cannot be replaced by return from `D3DKMTSubmitCommand` or return from cuDLA.
