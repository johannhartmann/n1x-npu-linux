# Region-5 allocation and initialization

> Historical research record. This document preserves observations from earlier
> bring-up stages, including superseded status claims and commands for older builds.
> Use [current status](../status.md) and [setup](../setup.md) for the tested behavior
> and current procedure. Command paths below are relative to the repository root.

The Linux bootstrap now has an optional region-5 setup matching the observed
Windows-default buffer layout. **It has not been run on hardware.** The raw
HAL-1 firmware accepts the complete original Windows-generated record in the
register/DMA model. This establishes registration and local header state, not
ring production/consumption or successful inference.

## Original Windows path

`emulate_region5_setup.py` starts with the pinned NVIDIA 617.42 KMD and executes
HAL-1 initializer `0x1951238` plus the original constructor-default block
`0x195d154..0x195d1ac`. It then executes buffer preparer `0xd43730`, original
allocators, provider `0xd470d0` and region builder `0xd49c20`. The allocator,
copy/cache, logging and OS boundaries are explicit stubs. The relevant original
HAL callback pointers are checked:

| Slot | Original function | Role |
|---|---|---|
| `+0x150` | `0xd45250` | allocate and clear the requested buffer |
| `+0x158` | `0xd470d0` | provide region layout and CPU/DMA pair |
| `+0x160` | `0xd49c20` | construct/send the region record |
| `+0x440` | `0x61a7f0` | return constant stride 52 |

The constructor's word `+0x480=1` enables the region-5 allocation. The preparer
requests **1,048,576 bytes** and stores its CPU/DMA pair at context `+0x4c8`.
The original allocator clears the full allocation. The preparer also executes
its default low-power/configuration allocations; their OS inputs are fixture
values, not live measurements, and no such configuration is submitted here.

For region 5, the provider returns:

- Total allocation: `0x100000` bytes.
- Descriptor offset: 0; descriptor length/ring offset: 16 bytes.
- Sizing stride: 52, obtained twice from the actual callback.
- Ring length: `floor((0x100000 - 16 - 8) / 52) * 52 + 8 = 0xfffd8`.
- Capacity according to that arithmetic: 20,164 strides plus an 8-byte header.
- Unused allocation tail after the ring: 24 bytes.

The calculation proves the provider's stride and capacity arithmetic. It does
not decode the 52-byte record fields or prove their producer/consumer semantics.

The 16-byte packed descriptor contains region ID 5, DMA base + 16 and ring size
`0xfffd8`. Windows sends command 11 with the **allocation base >> 8**, not the
ring address >> 8. Its builder supplies flags argument zero to the method
callback and requests cache visibility over the full 1-MiB allocation. All
bytes after the descriptor remain zero, including the host ring header.

The original preparer ignores an injected region allocator error and returns
zero; a null CPU result is also not propagated there. The later builder then
fails with `0x40` because its pair has no CPU address. With the region disabled,
the provider rejects it with `0x6d`; a send error propagates from the builder.
These are observed Windows behaviors, not Linux allocation policy.

## Raw HAL-1 registration test

The full original Windows-produced 1-MiB payload runs through raw HAL-1 reset,
trap entry, actual DMA functions, command handler and host ACK with unchanged
firmware code and no firmware function stubs. The explicit Linux test requests
response flags and observes `0xb02`, whereas the Windows builder's callback
above receives flags zero. That is a deliberate response-checking policy, not
an assertion that the Windows wrapper sends the same flags in every branch.

Firmware copies the descriptor to local `0x1859d0` and reads the eight header
bytes from ring address +0. A zero/zero host header becomes **8/8 in firmware-
local state at `0x1860f8`**, with no writeback to the host buffer in this test.
The host allocation stays byte-for-byte unchanged. This is not evidence that
the host header should be prefilled with 8/8 or that the ring can already be
consumed by Linux.

## Linux implementation

The separate bootstrap phase `n1x_boot_init_region5_once` requires immutable
`enable_region5=1`, exact audited machine/test kernel, both endpoint bindings
and this boot's verified DMA-word ping. The root-only trigger also requires
CAP_SYS_RAWIO. No automatic initialization or new register allowlist is added.

The phase revalidates DLA's translated/coherent DMA gate and 36-bit mask,
allocates 1 MiB, and rejects zero/unaligned addresses or any allocation extent
outside that mask before publication. It clears the full allocation and uses
the existing C region serializer with the exact provider-derived constants.
After dma_wmb, command 11 runs through the actual checked C control transaction.
Only a matching zero-status response sets `region5_registered`.

Allocation/range/serialization failures before a transaction free unpublished
storage. Once a transaction is attempted, the complete descriptor/ring buffer
is retained on **all** outcomes, including stale-response, ACK failure, firmware
rejection and success. The earlier upload already pins module/runtime lifetime.
A repeat trigger returns EALREADY. Registration is not proof of ring idleness,
DMA quiescence, safe free, task completion or full initialization.

Use the pending sequence in [Linux backend](linux-backend.md) only after manual
test-kernel boot and earlier hardware qualification. No bootstrap module or
firmware has been installed/loaded by these changes.

## Reproduction and validation

```sh
venv/bin/python scripts/emulate_region5_setup.py
venv/bin/python scripts/test_linux_region5.py
```

The Windows test verifies original allocation zeroing, provider sizing, actual
callback identity, the full builder record, cache extent, four failure controls
and the original-firmware registration/header path. The exact Linux phase is
compiled with the real serializer/control/sender/response code for 26 lifecycle
cases. At its publication boundary its **entire 1-MiB buffer** is compared with
the original Windows buffer, including untouched tail/header bytes. Fault tests
cover disabled/failed-ping gates, DMA extent, all ten transaction I/O failures,
stale/rejected/mismatched replies, early-only free and no repeat attempt.

Both kernel builds pass; the latest isolated target build and source/artifact
manifest are in `results/npu1-bootstrap-region5-build/`. All OS/device effects
in these tests are synthetic. Physical firmware identity/acceptance, DMA/IRQ
behavior, ring field semantics and production/consumption, later initialization,
recovery and numerical inference remain open.


## Recent-history software reader

The original Windows recent-history reader is now reproduced for immutable CPU
snapshots, including 52-byte records, a 50-record history limit and wrap copying.
It does not consume entries, and the record fields remain opaque. This is not
a live ring reader or completion proof. See [history evidence](region5-history.md).


## Isolated raw producer and wrap behavior

The raw HAL-1 producer is now exercised separately through original DMA code.
It publishes record data before the header under the explicit completion model,
but a wrap-path quirk writes a record that the Windows-matched history view does
not expose before the next call overwrites it. This does not establish behavior
of the opaque live image. See [producer evidence](region5-producer.md); no
physical ring, lossless event stream or completion claim is made.
