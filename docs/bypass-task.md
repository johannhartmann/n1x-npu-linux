# HAL-1 task completion without accelerator execution

The extracted Windows HAL-1 raw firmware, SHA-256
`2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997`,
implements descriptor flag bit 0 as an execution bypass. This provides a
transport/scheduler/postaction qualification path for a future physical driver.
It does **not** execute a network, produce a numerical inference result or
establish that the opaque SROOT-loaded image has identical code.

## Recovered path

The original executor at `0x1014f6` re-fetches the 60-byte task descriptor.
Instructions `0x101574..0x101584` load the word at descriptor offset 40, shift
right eight, mask bit 0 and branch to `0x10162e` if set. Thus the flag is bit 0
of the little-endian u16 at byte **41**. The bypass branch returns through the
original timing/result epilogue with the successful descriptor-read status.
It skips network/address-list loading and instruction execution. It still
performs control-register accesses before and after this branch.

The independently compiled GPL L4T `dla_os_interface.h` defines
`DLA_DESC_FLAGS_BYPASS_EXEC` as 1, and `offsetof(dla_task_descriptor, flags)`
is 41. The test pins the previously recovered header SHA-256
`e962bcaae697269981083196eed90a495949c636925d43ea4c398337de7c6162`.
This is a layout correspondence, not evidence of identical hardware semantics
or a live Windows task policy.

After a successful bypass execution, the original scheduler calls postaction
handler `0x103d56` and dequeues the task through `0x103f9c`. With both execution
and postactions successful, its path `0x10484c..0x104850` reaches `0x102206`.
That original routine performs these SWGEN0 writes in order:

| Logical offset | Value | Observed role |
| --- | --- | --- |
| `0xd24` | `0x40` | clear SWGEN0 mask |
| `0x4` | `0x40` | clear SWGEN0 status |
| `0xd20` | `0x40` | rearm SWGEN0 mask |
| `0x0` | `0x40` | assert SWGEN0 |

No additional SWGEN1 mailbox completion frame accompanies this successful
single-task bypass case. The earlier frame `0x702` is still only submission
acceptance. SWGEN0 has no task identifier in this test; association with a
particular task is justified here by there being exactly one outstanding task.
It must not be generalized to multi-task ownership or buffer reclamation.

## Linux serializer and verification

`n1x_dla_build_bypass_fenced_task_v2()` builds the existing 256-byte padded,
96-byte logical fenced v2 descriptor and sets only byte 41 to 1. It rejects
chains and keeps the existing address-list/fence alignment checks, even though
the observed bypass branch does not fetch the address list. It does no I/O,
submission, polling or DMA lifetime management. The optional native bootstrap
phase described below owns the one fixed descriptor; it exposes no arbitrary
model/task UAPI. Invalid inputs leave output unchanged; args may overlap
output because parameters are consumed before publication.

```sh
venv/bin/python scripts/test_bypass_task.py
```

The test executes original reset initialization, method-FIFO trap/return,
command-7 submission, scheduler, executor, postactions and dequeue without
firmware function stubs or code edits. Actual C descriptor serialization,
method submission and SWGEN1 capture/clear/ACK are used. Hardware reset,
DMA/IRQ registers, readiness and host memory remain modeled.

Four successful fence values (0, 1, 0x12345678, 0xffffffff) each produce one
`0x702` mailbox frame, one SWGEN0, an empty queue and a released firmware task
slot. Eleven original DMA commands comprise two 60-byte descriptor reads
(32/16/8/4 each), a 4-byte postaction table read, a 256-byte descriptor-cache
read and a 4-byte fence write. No network or address-list DMA occurs. The fence
DMA command and original completion polling precede SWGEN0 assertion.

Three negative controls distinguish important boundaries:

| Input | Mailbox frames | Fence | SWGEN0 |
| --- | --- | --- | --- |
| Same zero network, bypass flag cleared | `0x702`, `0x8003070a` | unchanged | absent |
| Bypass, malformed postaction list length 13 | `0x702`, `0x8007070a` | **written** | absent |
| Bypass, invalid descriptor version 255 | `0x90701` | unchanged | absent |

The malformed-list control is particularly important: a changed fence can
precede a later postaction failure. A Linux task implementation must combine
submission status, subsequent errors, task-associated completion/deadline and
ordering, rather than treating a fence value alone as successful work.

Six preflight controls and overlapping args/output are checked separately.
The existing task serializer and full response/scheduler regressions pass.
The task object compiles against both current and npu1 kernel headers; the
copied-source target manifest is `results/npu1-bypass-task-build/`.
No physical task was submitted, no IRQ requested and no module installed by
this addition. Physical boot/DMA/IRQ qualification and numerical execution
remain requirements for a usable Linux inference driver.


## Native one-attempt bootstrap phase

Immutable `enable_bypass_task=1` permits root/CAP_SYS_RAWIO to write `run` to
`NVDA200A:00/bypass_task`, after ping, region-5 registration, command-21 import
and command-23 fallback import have all succeeded in this boot. The phase is
not called automatically. It requires the existing exact-machine/test-kernel
and translated DLA DMA gates; it rejects pending SWGEN0 or SWGEN1 before
allocating or submitting anything.

One coherent 4096-byte allocation contains the descriptor at offset 0, padded
address list at 256, zero dummy network at 512 and fence at 768. The full
allocation must fit the 36-bit DMA mask and start at a nonzero 256-byte-aligned
IOVA. There are no task chains or user pointers. The descriptor timeout field
is zero; the recovered bypass path skips network execution, including its
normal timeout initialization, so this is not a physical timeout calibration.
The host deadline instead uses 5001 checks with 1000-us sleeps, as a bounded
Linux experiment. Delay/scheduling and the separate send budget mean this is
not a strict five-second wall-clock deadline.

The CPU initializes fence 0xdeadbeef, publishes with dma_wmb and sends method
0x307. Every polling iteration checks SWGEN1 first. Mailbox capture/clear/ACK
uses the existing C response helper; only command 7, matching descriptor data,
upper byte zero, status zero and message 2 establish acceptance. Descriptor,
execution and postaction errors fail the phase and preserve the raw captured
frame. Duplicate acceptance or other unexpected frames also stop the phase.
With dma_rmb/READ_ONCE, fence 0x12345678 is observed separately from SWGEN0.
Success requires all three: acceptance, an observed SWGEN0 and that fence.
Before clearing only SWGEN0 status, a second pending check gives newly arrived
mailbox errors priority on the next iteration.

The status fields distinguish attempted, raw reply validity/header/data,
accepted, signal_seen, fence, pending, verified, retained and error. `verified`
means this fixed qualification protocol was observed. It does not assert
whole-device quiescence, numerical results, arbitrary model support or absence
of every future asynchronous error. All backing stays retained on every path
after submission may be attempted, even after success, under the upload's
module/runtime references. Only unpublished preparation failures free it.
No automatic retry, reset or recovery is implemented.

`scripts/test_linux_bypass_task.py` compiles the exact phase with the real task
and address-list serializers, sender, response decoder and ACK helper. Forty-four
cases cover full 4096-byte payload comparison, prerequisites, stale IRQs,
allocation/mask/extent failures, all 14 success-path I/O failures, coalesced
acceptance/SWGEN0, timeouts with acceptance alone or signal without fence,
postaction failure after fence change, a later error arriving at the final
recheck, duplicate replies and malformed response fields. These device effects
are synthetic; the original-firmware path above remains a separate integration
test. No hardware task, IRQ registration or physical completion is claimed.

The updated full bootstrap module builds for both kernels; the copied-source
npu1 artifact is `results/npu1-bootstrap-bypass-build/`. The module is not
installed or loaded by these builds, and the existing test UKI/menu is unchanged.


## Exact native phase coupled to original firmware

`scripts/test_linux_bypass_firmware.py` closes the separation between the
native lifecycle harness and the raw-firmware scheduler test. It extracts and
compiles the **exact** `n1x_boot_bypass_task_once()` function with real Linux C
serializers, method sender, response capture/clear/ACK and decoder. Its modeled
kernel allocator gives that phase a coherent 4096-byte CPU buffer/IOVA; the
machine, DMA and prerequisite-phase gates are explicit successful shims.

Each case starts original raw HAL-1 reset code, runs a checked ping through the
original trap, then keeps that same firmware CPU/RAM instance for the native
phase. When native C polls its logical MMIO, the model advances the original
IRQ handler/scheduler, implements DMA device registers, and copies modeled
coherent RAM at the CPU visibility boundary. Firmware code is never patched
and no firmware function is replaced. New SWGEN1 yields are taken only after
the asserting store commits, avoiding replay of a store after the C ACK.

Four cases pass:

| Modeled device payload | Native result | Accepted | Verified | Task DMA commands |
| --- | --- | --- | --- | --- |
| Exact native payload | 0 | yes | yes | 11 |
| Version changed to 255 | -EREMOTEIO, `0x90701` | no | no | 4 |
| Bypass bit cleared, zero network | -EREMOTEIO, `0x8003070a` | yes | no | 13 |
| Postaction length changed to 13 | -EREMOTEIO, `0x8007070a` | yes | no | 11 |

Faults are deliberately injected into the modeled device-visible DMA payload,
not the native or firmware executable. The success path returns through the
original task-submission trap, reaches scheduler WFI with an empty queue, and
lets C observe acceptance/fence/SWGEN0 and clear SWGEN0. Error paths return to
the native caller immediately after capture/ACK; the firmware can still be
paused inside its original IRQ handler. The C state retains backing and refuses
another attempt in all four cases. This directly checks the lifetime rule that
ACK does not mean the handler returned or the device became quiescent.

The postaction-failure case has a changed fence in modeled device RAM while
the C phase correctly returns an error; its saved last CPU fence observation
can predate that write because the error path stops before another visibility
boundary. These are deliberately separate observations.

This verifies the actual native phase against the recovered raw protocol under
synthetic kernel/MMIO/FIFO/DMA/IRQ/coherency/handshake behavior. It still does not
prove physical DMA, interrupt delivery, opaque-image equivalence, task execution
on the NPU or numerical inference. No machine driver, boot artifact or firmware
installation is changed by the test.


## Completion boundary and repeated firmware submissions (2026-10-27)

The native/firmware integration test now includes the current shared task
runner and idle-drain helper. The earlier extraction started at the bypass
wrapper and had become incomplete when those functions were factored out;
it failed compilation. This harness regression is repaired, and the test is
included in `make test-p16-start` as well as `make test-vendor`.

The previous integrated success case advanced the emulator to scheduler WFI
before the native caller observed SWGEN0. That was insufficient evidence for
reclaiming memory at the actual completion signal. The new case stops the
original firmware immediately **after** the SWGEN0 store at `0x102228` commits,
with PC `0x10222a`. Stopping after the store also prevents replaying the signal
when the emulator resumes.

At that point, the bypass trace shows:

- all 11 task DMA operations have completed in the synchronous register model;
- the fence contains `0x12345678`;
- queue count at `0x180114` is zero;
- firmware task-slot in-use field at `0x18027c` is zero;
- the native runner can complete successfully before firmware reaches WFI.

The test then unmaps the entire device-visible task backing, resumes the
original firmware from `0x10222a`, and reaches WFI without another DMA command
or unmapped-memory access. The test repeats this for **32 submissions in one
firmware CPU/RAM instance**, reinitializing only the host harness between jobs.
No firmware reset, function stub or executable patch is used between tasks.
The existing hardware bootstrap still enforces its one-attempt policy; this
test does not silently enable repeated production submissions.

A negative control revokes the device mapping immediately after acceptance
`0x702`, when only four DMA reads have occurred. The original executor then
attempts another 32-byte descriptor read and the model rejects it. The native
caller returns `-EIO`, never reports completion and retains its CPU backing.
Thus acceptance and safe end-of-job observation are observably different.
Descriptor-version, execution and malformed-postaction error cases remain
covered and keep the native backing retained.

Local result: `results/native-fw-retirement-boundary.json`; UBSan counterpart:
`results/native-fw-retirement-ubsan.json`. All cases pass. This establishes the
raw firmware's bypass lifetime ordering under the modeled device. It does not
prove physical engine DMA quiescence, reset safety or error-path reclamation.
In particular, a failed task must not inherit the successful-task release rule.
