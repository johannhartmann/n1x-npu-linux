# Windows buffer release and the Linux retirement boundary

> Historical research record. This document preserves observations from earlier
> bring-up stages, including superseded status claims and commands for older builds.
> Use [current status](../status.md) and [setup](../setup.md) for the tested behavior
> and current procedure. Command paths below are relative to the repository root.

Research date: 2026-10-27. This investigation executes original Windows ARM64
instructions offline. It does not release live P16 DMA memory or change the
Linux driver's retention policy.

## Finding

The companion [submission trace](windows-submission.md) now executes the
runtime-to-cuDLA wait/submit/post-signal sequence. It demonstrates an error
return after a successful modeled submit boundary and locates the queued
WDDM synchronization layer; it does not qualify Linux DMA reclamation.

The inspected cuDLA backend delegates final allocation destruction to Windows
with **both `AssumeNotInUse` and `SynchronousDestroy` clear**. Consequently,
returning from cuDLA memory unregister is not evidence that physical backing
has already been reclaimed or that the accelerator is idle.

Microsoft documents that `AssumeNotInUse = FALSE` defers destruction until
previously queued commands finish, while `SynchronousDestroy = FALSE` permits
return before underlying memory is reclaimed. This explains an important
missing layer in a direct translation of the Windows userspace code: Windows
owns deferred destruction. Linux must supply its own verified job/resource
retirement contract. [Microsoft's flag definition](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dukmdt/ns-d3dukmdt-_d3dddicb_destroyallocation2flags)

This is a conclusion about the inspected path and the documented Windows
contract. It does not prove which backend a live Windows N1X selects, nor
prove that a firmware fence alone establishes the corresponding Linux boundary.

## Pinned input and call chain

Input: NVIDIA 617.42 `sources/nvidia/Display.Driver/nvcudla.dll`, SHA-256
`8da1b42d35aa79c71e3102f95dde30a75dbe498983d386b3cb68082f6b467ba0`.
All addresses below are RVAs at preferred image base `0x180000000`.

Radare2 locates these original functions; Unicorn executes the export, error
gate, backend, registration trees, reference-count changes, API resolver and
KMI envelope builders. Windows initialization/locks, runtime unregister and
memory destruction, the GPU-address getter, CRT deallocation and D3DKMT calls
are explicitly modeled boundaries.

| Stage | RVA / structure |
| --- | --- |
| Export `cudlaDrvMemUnregister` | `0xdc20` |
| Common error inspection | `0x273b0` |
| Inspected device vtable / unregister slot | `0xc2628 + 0x20` -> `0x20560` |
| Runtime adapter vtable | `0xc2820` |
| Unregister dispatch | adapter slot `0x20` -> `0x2d000` |
| Read memory descriptor | adapter slot `0x40` -> `0x2dec0` |
| Destroy registration/backing reference | adapter slot `0x48` -> `0x2dee8` |
| Kernel-mode-interface vtable | `0xc2078` |
| Evict / free GPU VA / destroy allocation wrappers | `0x8c70` / `0x90d8` / `0x86d0` |
| Dynamic D3DKMT resolver | `0xb8b8` |

The device factory writes the inspected device vtable at `0xd29c..0xd2a4`.
The alternative factory path writes vtable `0xc2568` and uses unregister
`0x134c8`; that alternative is not executed by this test. Do not label these
two factory branches as particular live N1X modes without further evidence.

## Error gate

For an initialized device with marker `0x11a9a` at context `+0x38`, the export
calls `0x273b0` before backend unregister. The gate reads five pointers at
context `+0x40..+0x60`, loading a 16-byte record from each and examining its
unsigned 16-bit field at `+14`.

| Field value | Returned cuDLA status |
| --- | --- |
| 0 | Continue; zero if no error is encountered |
| 3, 6, 8, 12 | `0x40000001`, `0x40000002`, `0x40000003`, `0x40000004` respectively |
| 14 | Remember `0x40000005`, but continue scanning |
| 15, 16 | `0x40000006`, `0x40000007` respectively |
| Other nonzero values | 7 |

Except for value 14, nonzero values return immediately. This is ordered
inspection, not a maximum-severity reduction. A later nonzero value can
replace the remembered result for 14. The test does not assign firmware-error
names to these fields or assume identity with our mailbox error codes.

A nonzero gate result prevents runtime unregister, reference-count changes
and all tested frees. Unknown/null memory and null device handles also fail
without freeing registered storage.

## Two-reference and last-reference release

The device registration tree at context `+0x110` maps a key to a block record.
The adapter has a second tree at `+0x40` for shared backing records. These
counts are distinct from GPU residency references.

In the tested normal registration path, the adapter invokes runtime vtable
slot `0xa8` with the memory object, 32-bit offset and zero fourth argument.
Registration kind 2 invokes the imported `unregisterTaskStatistics` instead.
Failure returns status 7 without removing the registration. Success proceeds
through imported `destroyMemory` and decrements the shared backing count.
The imports are modeled; their own internal synchronization is not established.

With two backing references, the count becomes one. The per-registration block
and tree node are freed, with no D3DKMT call in the executed cuDLA portion.
The modeled runtime imports could have additional behavior.

With one backing reference, the count reaches zero and the original KMI
wrappers produce this sequence:

1. `D3DKMTEvict`: one allocation handle, flags zero.
2. `D3DKMTFreeGpuVirtualAddress`: adapter handle, GPU virtual address, size.
3. `D3DKMTDestroyAllocation2`: device and resource handles, null allocation
   list, allocation count zero, **flags zero**.

The original resolver establishes the function-pointer names at globals
`0xcfa88`, `0xcfb00`, and `0xcfb40`; they are not inferred from wrapper names.
The test checks all relevant envelope fields and records their raw bytes.
For the chosen sentinel resource, the 24-byte destruction envelope equals
`struct.pack('<IIQII', 0x55, 0x456, 0, 0, 0)`.

Eviction decrements residency accounting; it is not itself documented as
physical destruction. [Microsoft D3DKMT_EVICT](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_evict)
The subsequent zero destruction flags supply the deferred-reclamation
contract described above. No standalone polling loop for DMA idle appears
in the executed cuDLA/KMI instructions, but the modeled runtime imports and
Windows kernel behavior remain outside the test.

## Failure behavior and Linux implications

Injecting `0xc0000001` at any of the three D3DKMT boundaries stops subsequent
kernel calls and returns `0x7fffffff`. Nevertheless, the inspected adapter
removes its shared backing record and frees the per-registration block. The
outer device registration remains present. This is partial cleanup, not a
transactional rollback. The test records the state; it is not a live Windows
vulnerability claim and does not establish what recovery Windows performs.

For Linux, distinguish a client releasing a handle from the kernel retiring
the underlying DMA allocation. A close/release operation must not authorize
reuse while a job may still access backing. Preserve execution references
until a verified successful retirement boundary; after uncertain errors,
preserve them until a separately verified reset/quiescence procedure.
Do not translate Windows unregister success into `dma_free_coherent()`.

The existing [firmware retirement tests](bypass-task.md#completion-boundary-and-repeated-firmware-submissions-2026-10-27)
show the successful firmware tail no longer accesses the modeled job after
SWGEN0. That remains stronger task-specific evidence than the userspace
unregister return, but its synchronous DMA/engine model does not establish
physical ordering. The [normal KMD completion reader](windows-kmd-task.md)
now connects the device-written semaphore to job selection and Windows
scheduler notification. Its [outstanding-job recovery helper](windows-kmd-task.md#software-retirement-is-not-device-completion)
can also advance software completion/cleanup counters without reading a new
device completion. These paths must remain distinct. Physical repeated-job
qualification, device ordering and error/reset quiescence remain missing;
the Linux retained-memory policy is unchanged.

## Reproduce

```sh
venv/bin/python scripts/inspect_cudla_release.py > results/cudla-release.json
```

The check exercises 104 gate cases and 12 export-to-release scenarios,
including normal/statistics unregister, missing handles, runtime failure,
shared backing, last-reference cleanup and each failing D3DKMT stage. It runs
without root or hardware access and fails on any unexpected imported API.
Original model execution, the runtime imports' internals and actual Windows
kernel allocation destruction are not emulated by this check.
