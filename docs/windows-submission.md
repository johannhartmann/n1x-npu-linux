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

The companion [original command-builder test](#original-command-builder)
now removes that modeled builder in five complete runtime-to-cuDLA submission
cases, while retaining the isolated 14-case transport/error regression above.

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

The [concrete DPC trace](hfrp-irq.md#concrete-dpc-path-and-primary-interrupt-acknowledgment-2026-10-27)
now follows IRQ acknowledgment through deferred SWGEN0 processing. That path
only reaches the internal HAL counter update, without an observed task ID or
WDDM completion notification. The [KMD task replay](windows-kmd-task.md) now
executes the separate higher-level caller `0xdd6888`, envelope `0xdd22c8`,
command `0x35800001` handler and HAL direct-send branch. It also verifies the
Windows postaction producer against the original firmware consumer. Generic
object lookup, OS scheduling and physical retirement remain unproven.

The [virtual-submit translation replay](windows-kmd-task.md#runtime-private-data-to-concrete-npu-job)
now feeds this runtime's actual private block into the original NPU-class KMD
dispatcher and metadata/job producers. It distinguishes the 852-byte UMD
payload from the expanded 1,424-byte kernel metadata and finds no per-job APC
callback on this path. The user/kernel scheduling transition remains modeled;
this is not a live Windows trace or a physical retirement guarantee.

## Original command builder

On 2026-10-28, `scripts/inspect_runtime_command.py` executes the original
runtime builder at `0x743a8`, its three helpers, registry lookups and memset.
The input DLL hashes are the same as above. Reproduce without hardware access:

```sh
venv/bin/python scripts/inspect_runtime_command.py > results/windows-runtime-command.json
```

There are 39 cases: 34 isolated command-builder cases and five combined
runtime/cuDLA submission cases. The latter execute `0x75858 -> 0x743a8` and
the original KMI envelope builders in one emulator address space, reaching
modeled D3DKMT calls with the command address produced by the original builder.
They cover success, slot 31 wrapping to zero, map failure before submission,
OS submission failure and post-signal failure after submission. They do not
replace the OS boundary or execute the kernel/firmware consumer.

### Memory and command layout

The selected context slot contains six QWORDs:

| Slot offset | Observed use |
| --- | --- |
| `0x00` | Owner pointer; owner `+0x18` is the command-memory registry key |
| `0x08` | Descriptor offset in command memory |
| `0x10` | Address-list offset |
| `0x18` | Preaction-region offset; helper clears `0x600` bytes |
| `0x20` | Postaction-region offset; helper clears `0x500` bytes |
| `0x28` | Sync-object pointer used by the surrounding submission path |

Lookup `0x2cc30` resolves the command-memory key. The returned object's virtual
slot `+0x10` is invoked with mode 2 and an output CPU-address pointer; slot
`+0x18` receives the mapped CPU address during cleanup; slot `+0x38` supplies
the device base. These virtual methods remain modeled. Their names here
describe observed use, not verified platform allocation/cache semantics.

The builder emits this packed 64-byte **user-mode command descriptor**:

| Offset | Type | Value |
| --- | --- | --- |
| `0x00` | u8 | 2 |
| `0x01` | u16 | Preaction byte length, including terminator |
| `0x03` | u64 | Preaction device address |
| `0x0b` | u16 | Postaction byte length, including terminator |
| `0x0d` | u64 | Postaction device address |
| `0x15` | u64 | Address-list device address |
| `0x1d` | u16 | Address count |
| `0x1f` | u16 | 1 if the low byte of the builder flag argument is nonzero, else 0 |
| `0x21` | u32 | Copied task field `+0x19e18`; semantic meaning not established |
| `0x25..0x3f` | bytes | Zero |

The command output address is the memory device base plus slot `+8`; length
is the supplied task count shifted left by 6. The surrounding runtime rejects
counts other than one. This descriptor is distinct from both the `0x354`-byte
WDDM private data and the firmware task descriptor. A native driver must not
send it straight to firmware on the strength of this test.

Address helper `0x738f0` reads task DWORD `+0x10` and 16-byte records at
`task+0x1e18`: memory key (u64), offset (u32), and an unused trailing DWORD on
this path. It emits one u64 per entry: resolved device base plus zero-extended
offset. A zero key takes a separate path with base zero. Nonzero keys are
resolved by original lookup `0x2c8b8`; object kind 1 then uses registry lookup
`0x2cc30`, kind 2 uses `0x2cd10`. The test constructs these registry trees but
executes their actual lookup code. Unknown keys return status 4; an unsupported
kind returns status 2 in the tested paths. This is not a complete validation
or bounds-checking guarantee for arbitrary inputs.

### Action streams

Preaction helper `0x73ed8` and postaction helper `0x73b68` emit the following
byte sequences. All multibyte fields are little endian. The 16-byte source
records hold a memory key, u32 offset and u32 value; addresses resolve through
the same kind-1/kind-2 registries. Values in 11-byte actions truncate to u16.

| Stream | Count byte in task | Record-array offset | Emitted action |
| --- | --- | --- | --- |
| Pre | `0x0a` | `0x1418` | `c0`, u64 address, u16 value |
| Pre | `0x0b` | `0x1618` | `c1`, u64 address, u16 value |
| Pre | `0x0d` | `0x1a18` | `87`, u64 address |
| Post | `0x0c` | `0x1818` | `c1`, u64 address, u16 value |
| Post | `0x0e` | `0x1c18` | `87`, u64 address |

The pre helper then examines the existing 80-byte pre-fence array. Type 4,
subtype 1 emits `80`, u64 from record `+0x40`, u32 from `+0x48`. Other
subtypes are skipped here; subtype zero is handled by the surrounding WDDM
wait path. Invalid type returns status 2. Both action streams end in byte zero.
The separate task post-fence array is handled by WDDM signaling, not appended
by this postaction helper. The opcode bytes alone do not establish the exact
firmware operations; translation and consumer validation remain separate.

Tests compare complete descriptor and action bytes, >32-bit device addresses,
high-bit u32 offsets, truncation of action values, both object kinds, 31 inline
pre-fences, and 32 records in each action array separately. They verify cleared
stream tails and untouched adjacent sentinels. Those constructed counts are
test coverage, not a recovered general API limit.

### Cleanup and Linux implications

A successful build has four map/unmap pairs: the outer descriptor mapping
and one pair for each helper. Each helper propagates its unmap error only if
there was no earlier error. The outer builder always discards its final unmap
return status. Mapping failures still lead to cleanup with the initialized
zero CPU pointer; the modeled virtual method accepts that pointer. The test
does not establish how the real method treats it.

Failure injection checks all four map and unmap positions. Helper failures
leave the outer command-address/length outputs untouched, and combined tests
confirm that a builder failure prevents D3DKMT submission. Successful command
generation followed by a failed post-signal call still represents an attempted
submission and must not be retried automatically.

For Linux this recovers the missing userspace command format and separates
inline action streams from OS synchronization. It provides concrete inputs
for tracing the KMD's command-to-firmware transformation. It does **not** prove
that a CPU unmap releases a device mapping, that an unmap means DMA is idle, or
that physical backing can be reclaimed. Native DMA retention remains unchanged.
