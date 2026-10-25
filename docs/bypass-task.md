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
submission, polling or DMA lifetime management and is not exposed through a
native task UAPI yet. Invalid inputs leave output unchanged; args may overlap
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
