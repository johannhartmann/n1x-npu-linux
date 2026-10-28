# Experimental native DLA runtime

The module now contains an optional Linux DRM accel interface, a small native
C client and a Python binding. These components build and pass offline tests;
**the accel interface has not yet run on the physical P16**. The recorded
physical ReLU success still belongs to the earlier sysfs probe build.

This is a step from the manual probe toward a reusable driver, not a completed
production runtime or NVIDIA cuDLA/TensorRT compatibility layer. It accepts a
prepared N1X model image and task-local address ranges, not arbitrary ONNX or a
TensorRT engine. The [native loadable loader](loadable-runtime.md) now materializes a checked
NVDA 0.9.0 subset in userspace. Compiler target coverage and broader loadable
features remain separate work.

## Device interface

`enable_accel=1` at explicit module load registers `/dev/accel/accelN` using
`DRIVER_COMPUTE_ACCEL`. Registration does not upload firmware or submit a task.
The existing audited startup sequence must have completed through the bypass
probe before EXEC can use the backend. The default parameter is false.
The implementation is in `driver/n1x_dla_accel.c`; the experimental ABI is in
`include/uapi/n1x_dla_accel.h`.

All three ioctls require `CAP_SYS_RAWIO` on every call, including when an open
file descriptor is passed to another process:

| Operation | Contract |
| --- | --- |
| INFO | ABI version, image/address/job limits, currently retained DMA bytes |
| EXEC | Snapshot a prepared image and relative ranges; construct one task; synchronously return job ID, execution status and completion flag |
| READ | Copy a bounded range of a successful job's image back to its owning open file |

The kernel creates all descriptor/address-list IOVAs. It reserves an 8192-byte
prefix containing the task at offset 0, fence at 256 and address list at 512.
Payload offsets are relative to the image after that prefix. The descriptor is
an arithmetic fenced-v2 task, with one-second firmware timeout field and the
existing bounded native method/completion polling. Job IDs are distinct from
firmware fence values. The queue is shared with sysfs and serialized by the
same bootstrap lock.

The image's embedded instruction streams are not sandboxed. Checking relative
ranges does not validate their register access or all indirect DMA references.
This is why the interface remains a trusted-code experiment requiring RAWIO;
it is not ready for unprivileged inference or mutually untrusted clients.
There is no writable mmap, physical-address API, DMA-BUF import or CUDA-pointer
registration.

## Lifetime and failures

The driver frees a job only when submission was never attempted. Once a method
may have reached firmware, all of its DMA backing remains allocated until
reboot, including after success, failure, process exit or a failed ioctl
copyout. Closing a file removes access to its result IDs but does not free DMA.
Another open file cannot acquire those IDs, even if the kernel reuses the old
`drm_file` address. Startup already pins module and runtime-PM lifetime before
any possible accel submission.

The device limits retained allocations to 64 MiB and 128 jobs; each image is at
most 32 MiB with 1..512 address ranges. These are bounded bring-up limits, not a
reclamation implementation. EXEC snapshots the range metadata and payload;
only the private kernel copy is visible to firmware. Unknown flags/reserved
fields and out-of-image ranges fail before submission.

A successful syscall can contain a negative execution status and a retained
job ID. The C client returns that negative status while preserving the result
structure. A syscall failure, especially EFAULT after execution, may leave a
retained job without returning its ID. **Do not retry EXEC automatically.**
The runtime does not retry EINTR or any other error. READ requires successful
acceptance/fence/SWGEN0 verification and can only return the caller's payload,
not the kernel prefix or allocation padding.

Safe reclamation, reset/recovery, asynchronous scheduling and suspend/resume
remain open requirements. The existing driver still rejects suspend after
startup. No service or autostart is installed by this change.

The shared internal job executor now has separate `n1x_dla_job_start()` and
`n1x_dla_job_poll()` operations. Start publishes and submits once, then reserves
the queue for the exact job object. Poll consumes at most one mailbox response
without sleeping and returns `-EINPROGRESS` until a terminal result; the cumulative
5001-poll budget cannot restart on each call. Another job cannot submit, poll or
terminate the owned queue. A caller of the split interface must also enforce a
wall-clock deadline; polling count alone does not schedule future calls.
`n1x_dla_job_fail()` quarantines an abandoned/expired job's queue in software,
without claiming hardware cancellation or freeing DMA. Terminal errors retain
the existing poisoned-queue policy. Pre-submission publication failures release
ownership without poisoning; no method was attempted in that case.

The synchronous `n1x_dla_job_execute()` used by sysfs and accel EXEC now wraps
those operations with the same 1-ms polling delays. The public accel ABI and
memory retention policy are unchanged. This split provides the ownership boundary
used by the private checkpoint controller below. The mock lifecycle suite covers
81 cases. The original-firmware harness
also drives start and poll as separate native calls for 32 successive jobs on
one firmware instance, and verifies a postaction-error case. Those tests use
modeled MMIO, DMA and interrupt delivery, not the physical P16.

The [active firmware preemption tests](contract.md#preemption-after-the-original-scheduler-starts-an-arithmetic-task)
now distinguish a completed task from a failed context-save attempt under
modeled engine IRQs. Both can report the same preemption message, and neither
emits the usual task SWGEN0 in that sequence. The subsequent
[checkpoint tests](contract.md#original-firmware-checkpoint-save-and-restore)
exercise successful firmware save/restore control flow with explicit synthetic
engine events and separate checkpoint storage. Engine context transfers and
physical DMA quiescence remain unqualified. This is not yet a Linux cancellation
operation: a future cancel path must reconcile execution errors, checkpoint
state and task completion rather than turning message `0x120b` into successful
inference or immediately freeing DMA.

The [native v2 context transport](contract.md#native-version-2-context-transport)
now has C serializers and original-firmware save/restore control-flow coverage.
It requires descriptor bit 2 to load a separate context header, bit 1 to permit
restore, a 32-byte checkpoint and the compiler's engine-context backing. These
helpers are not yet used by EXEC and do not change its retention policy or add
a cancellation API. Native v2 checkpoint bookkeeping also differs from the
Windows v3 UMD path; host deadlines must remain independent of that checkpoint.
The [native preemption request builder](contract.md#native-preemption-request-serialization)
also emits the complete padded method-18 payload in C. The native firmware
tests no longer depend on Windows code to generate submitted control packets;
the Windows builders remain comparison oracles. Submission/ACK handling,
cancellation state and physical verification are not enabled by serialization.

The private `n1x_dla_preempt()` helper now adds bounded method-18 submission and
response consumption around those packets. It rejects stale checkpoint/fence
state and pending task/response IRQs before submission, sends once, and checks
both the acknowledged `0x120b/data=0` message and the acquired DMA snapshot.
Checkpoint-only and task-fence-only observations are distinct outcomes; neither,
both, an invalid marker or a mismatched preemption fence fail closed. Firmware
errors and transport/ACK errors propagate without retry. Each of the method
availability and response phases permits at most 5001 polls with 1000-us delay
intervals (at most ten seconds of requested delays combined, excluding I/O).

`scripts/test_native_preempt.py` drives this compiled C helper against the
original HAL-1 firmware with synthetic engine IRQ/status and RAM DMA. Fourteen
cases cover checkpoint save, save failure, completion with status 1/5, completion
between preflight and command delivery, disabled preemption and missing engine
events across both tested engine IRQ bits. Another 52 cases cover stale values,
malformed responses, every transport/snapshot failure edge and bounded waits.
The original arithmetic executor reports -16 in the save-failure fixture while
still emitting `0x120b`; the native host correctly returns an error rather than
claiming a checkpoint. No numerical computation or physical engine-context
transfer is modeled. The original idle producer at `0x107318`, run with a
synthetic timer and intercepted outbound-notification boundary, supplies the
idle fixture `{12, 0}` (decimal 12, hexadecimal `0x0c`). The helper tolerates
that exact notification; the distinct value `0x12` is rejected. This corrects
the initial host helper and its mirrored mock fixture, which both used `0x12`.

This helper is built as an independent kernel component, not linked into the
bootstrap module or called by EXEC. Its caller must own a single accepted task,
its context and request allocations, serialize response consumption, and
quarantine uncertain execution. Even a successful observation does not permit
freeing DMA or automatic resume. Physical qualification and integration with
the public execution path remain necessary before exposing cancellation to applications.

### Private job preemption and resume controller

`driver/n1x_dla_job_control.c` now connects the preemption primitive to an owned
native job. It is compiled for offline/kernel checks but is not linked into the
bootstrap module or called by the public EXEC interface. Its caller must declare
the job resumable before first submission and supply an immutable context-enabled
descriptor with restore permission, retained checkpoint and compiler engine
context storage. Merely setting the C flag is not descriptor validation.

On checkpoint completion the job stays attached to its queue; normal polling
and another submission are refused. Resume verifies the checkpoint marker,
unchanged task-fence state and last preemption sequence, then sends the same
descriptor again. It does not call the publication callback, change the input,
reset the cumulative poll budget, or allocate another task fence. Firmware owns
consumption/clearing of the saved marker. The owner must enforce an absolute
wall-clock deadline across pauses; polling and individual bounded commands do
not supply that scheduling policy.

If the original task completes during preemption, the exact control response,
sequence fence and task fence form an alternative completion protocol. The
controller checks for late mailbox errors and records `preempt_completed`;
SWGEN0 is cleared when present but is not fabricated when the preemption path
suppresses it. This is protocol completion, not numerical or physical-quiescence
proof. A failed save, stale resume state, late error or uncertain command delivery
quarantines the queue without freeing any memory or retrying. A preflight
EBUSY/EALREADY with no preemption attempt leaves the running job available for
ordinary polling. Other invalid API/ownership calls perform no device I/O.

`scripts/test_job_preemption.py` drives the actual C start/poll/preempt/resume
functions against the original firmware in 48 scenarios. It covers both engine
IRQ bits, two save/restore cycles on one job, completion races, failed saves,
disabled preemption, timeouts, late-error/idle transport injections, stale
checkpoint/sequence/task-fence values, resume delivery failure, exhausted poll
budget and caller abandonment. The native publication callback executes once;
descriptor bytes, input data and unique task fence remain unchanged across
resume, and both original restore IL records execute. Arithmetic and physical
engine-context transfers are unmodeled; output remains poisoned deliberately.
The repeated-cycle coverage now includes a second request during either restore
record. The original firmware transitions from restore state 4 to state 5,
waits for the issued restore record's completion, republishes a checkpoint and
finishes preemption without running another engine-save sequence. Subsequent
resume executes both restore records again. These tests inject one completion
IRQ for the already-issued record, not the three-IRQ sequence used for saving
an active arithmetic task. Engine-context DMA remains unmodeled.

The acceptance reply precedes consumption of the resume checkpoint. The native
controller tracks that known window with `restore_pending`: marker 1 with an
unsignaled task fence returns EBUSY without a preemption command, sequence
advance or queue poisoning. Normal polling continues under the original host
deadline. Once the marker is consumed, preemption can proceed even while a
restore record is in flight. Corrupt markers, contradictory task completion and
snapshot errors still quarantine the queue. This replaces the earlier false
ESTALE classification immediately after resume acceptance; it does not make
arbitrary stale markers on a newly submitted task acceptable. Physical engine
recovery and numerical correctness across these interruptions remain unqualified.

The [Windows release investigation](windows-buffer-lifetime.md) now traces
cuDLA unregister through error gating and last-reference D3DKMT destruction.
Its zero destruction flags explicitly allow Windows to defer reclamation.
An unregister return is therefore not a portable DMA-quiescence guarantee;
the Linux retention policy is unchanged by that offline evidence.
The [submission trace](windows-submission.md) additionally verifies separate
WDDM wait, submit and post-signal operations, including failure after the
submit boundary. The runtime's prohibition on automatic retries also applies
to this kind of partially successful submission.

## Build and offline checks

```sh
make runtime
venv/bin/python scripts/test_dla_runtime.py
venv/bin/python scripts/test_linux_accel.py
make test-p16-start
make -C /lib/modules/7.2.8-23-omarchy-p16-asus-npu2/build \
  M="$PWD/driver" n1x_npu_bootstrap.ko
```

`test_dla_runtime.py` checks the actual C client's driver/version lookup,
C/Python structure layout, error propagation and lack of automatic retries
using wrapped syscalls. `test_linux_accel_backend.py` covers 15 readiness,
locking and error-propagation cases in the actual bootstrap callback.
`test_linux_accel.py` compiles the actual ioctl and
close handlers against modeled user-copy/DMA/locking boundaries. It exercises
32 EXEC cases plus INFO, READ, ownership and close behavior. Its execution
backend is synthetic; it does not claim physical device validation.

The latter test also prepares changing FP16 input through the Python tool,
captures the exact kernel-produced descriptor/address list, and feeds that
payload to the original HAL-1 firmware reset/trap/scheduler path. Firmware
accepts the task and programs the expected engine addresses. Engine arithmetic
is not emulated. The native execution loop and error behavior are separately
covered by the existing original-firmware integration test. Tests pass with
UBSan as well.

## First hardware qualification (not yet performed)

Use a fresh boot of the audited `npu2` kernel. The already-loaded bootstrap
cannot be replaced in place. Preserve the prior working boot/module artifacts.
After confirming the new build and current board inputs:

```sh
powerprofilesctl set performance
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through bypass_task --enable-accel --log "$PWD/results/NEW-accel-start.jsonl"
```

Select the node whose DRM driver name is `n1x-dla`; the C client verifies that
name and ABI version rather than assuming any particular accelerator index.
The kernel accepts general prepared images. The supplied qualification CLI
uses the one hash-pinned, compiler-produced ReLU image with changing input:

```sh
pkexec /usr/bin/python3 "$PWD/runtime/run_relu.py" \
  --device /dev/accel/accel0 \
  --template "$PWD/results/p16-relu-template.bin" \
  --input "$PWD/results/input-a.fp16" "$PWD/results/input-b.fp16" \
  --output-dir "$PWD/results/NEW-accel-relu"
```

Each input is exactly 3136 bytes: 1568 little-endian FP16 values in the checked
CHW16 layout for shape `[1,32,7,7]`. The numerical acceptance CLI currently
requires normal finite nonzero inputs; zero/denormal/NaN handling needs separate
qualification. The graph and its weights/instructions remain the pinned vendor
artifact. Output is poisoned before each submission; returned output must
match the independent bitwise FP16 ReLU reference and input must be unchanged.
The CPU computes only the reference, never the returned inference result.

The CLI validates all inputs and checks the retained-memory budget before
submission. It creates a fresh output directory, writes/fsyncs attempt records
before each submission, and records job IDs, outputs, hashes and errors. It
stops at the first failure without retry. The template's first 8192 bytes are
replaced by kernel-owned transport structures; the model payload offsets and
logical address lengths come from the verified loadable.

No hardware execution, module replacement or reboot was performed while adding
this interface. A passing build or emulated scheduler acceptance is not evidence
that `/dev/accel` numerical inference already works on the P16.
