# Builtin diagnostic selection, buffers and result boundary

The pinned Windows `nvdla_runtime.dll` contains a 5,939,144-byte builtin NVDA
loadable at RVA 0xa6610. Runtime SHA-256:
`c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b`.
Loadable SHA-256:
`51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543`.
This is the same input used by the existing nine-allocation scheduler fixture;
no additional model or vendor binary is distributed with this repository.

## Original Windows selection

`scripts/inspect_diagnostic_contract.py` executes the complete normal-return
paths of original ARM64 function **0x15eb8**, called by exported
`IRuntime::appendDiagnosticLoadable` at 0x262d0. Lock/log/CFG and the final
model-parser callback are explicit emulation boundaries; the latter is not a
real model parser or device operation.

The original function locks context+8, logs, reads the u32 at context **+0x98**,
and selects its builtin payload only when that field is **1**. It calls
context vtable **+0x218** with context, the actual pointer `base+0xa6610`, size
`0x5a9fc8` and the caller's output-handle pointer. The test hashes all bytes
read through that original pointer. Selectors 0/2/3/4/UINT32_MAX return 2
without calling the model parser. A modeled model-parser error or logging
error 0x65 propagates; normal paths unlock before returning. Eight cases cover
these branches. Lock failure/exception unwinding is not emulated here.

The selector is an observed runtime field. It is not automatically the same
identifier as the KMD hardware-profile number or raw firmware HAL selector.
The model handle 42 used by the test is a synthetic parser output, not a live
Windows or Linux allocation.

## Observed buffers and table slots

The bounded existing NVDLA-v1 projection exposes nine memory/address entries.
The six root-vector-6 tables have raw names and scalar slots consistent with
these memory entries. Their complete modern schema is still unverified; the
following is a checked byte/size correspondence, not a full parser validation:

| Raw table name | Slot 1 u16 | Slot 2 u16 / projected memory ID | Slot 3 u64 / bytes |
| --- | --- | --- | --- |
| task0_stats | 0 | 3 | 27264 |
| working_buffer | 1 | 4 | 5729504 |
| scratch_buffer | 2 | 5 | 2471680 |
| positive | 3 | 6 | 27264 |
| negative | 4 | 7 | 27264 |
| sdl_out | 5 | 8 | 32 |

The seven content blobs are negative, positive, task0-addr0, task0-db, task0-ib,
task0_stats and working_buffer. Scratch and sdl_out have no initialized content
blob. The audit checks original blob hashes, full fixture allocation extents,
content placement and address-list sizes. The current fixture requires
8,428,800 bytes including its alignment/cache padding for the existing HOST
base; this is not a claim about Windows allocation overhead.

`negative` is 27264 zero bytes. `positive` and `task0_stats` share their first
**17604 bytes**: LE u32 25, followed by 25 raw name-bearing blocks at stride
704. Names include fi_sdp1, fi_conv1 and fi_rubik0_concat_7x7x9_int8_mc2ub.
They first differ at byte **17688**. An aligned-tail interpretation involving
per-operation result records is a hypothesis; record field meanings, CRC
polynomial/coverage and the actual comparison algorithm have not been proved.
The entire positive blob therefore cannot yet serve as an unquestioned
byte-for-byte numerical oracle for a future statistics buffer.

In particular, an initially zero 32-byte sdl_out buffer is not a successful
health result. The two output-writing IL blocks below now establish a raw LE-u32 write;
the full result schema and hardware comparison meaning remain unknown. Neither
its name nor an accepted task/fence alone establishes numerical correctness.

## Official API correspondence and remaining execution work

NVIDIA documents an internally supplied diagnostics module and a dedicated
submission flag. Applications supply queried output buffers, while the
builtin diagnostics do not take input tensors. This supports investigating
the Windows embedded model as a hardware-test path; it does not establish N1X
Linux driver support or this binary's precise output layout. See the official
[cuDLA module and task APIs](https://docs.nvidia.com/cuda/archive/13.4.2/cudla-api/cudla_api/group__CUDLA__API.html).

The existing original-firmware fixture reaches its first engine wait, rather
than completing accelerator work. The raw output writer/software assertions are now reconstructed below. Next
requirements are the hardware comparator meaning, actual block selection,
verified engine IRQ/readiness and
physical execution after test-kernel/DMA qualification. Numerical results must
be checked independently of transport completion. This audit does not write
registers, bind a driver, change boot configuration or submit a hardware task.

```sh
venv/bin/python scripts/inspect_diagnostic_contract.py
```


## Original result assertions and output writer

The instruction blob contains two seven-instruction (112-byte) blocks at
**0x1e500** and **0x1e700**. Both read internal register offset **0x13124**,
assert a predicate and copy that register to address-list entry **8**, byte
offset zero. This maps to the projected sdl_out allocation. Internal register
offsets here are the firmware's engine aperture, not a host DLA remapper API
or permission to access a physical register from Linux.

The first block writes 0 to internal offset 0x12004 and uses opcode 4,
predicate 1, full mask 0xffffffff, shift 0, expected 0. The second writes 1
to 0x12004 and uses predicate 4 with the same mask/shift/expected. Both copy
instructions are exactly `23 00 08 00 00 00 00 00 24 31 01 00 00 00 00 00`.
No physical meaning is assigned to selector 0/1 yet.

`scripts/test_diagnostic_output.py` runs each exact seven-instruction block
through original HAL-1 dispatcher **0x102abe**, assertion handler **0x102a26**,
predicate **0x102800** and output handler **0x1029b0**. Original reset/trap/ping
initialization precedes it; original address-list setup and DMA functions
remain unchanged, with no firmware function stubs. Test context, register
values and DMA device effects are synthetic. Blocks are invoked directly;
the full diagnostic scheduler has not selected or completed them in this test.

Seven cases establish:

| Block | Predicate recovered by execution | Modeled register values | Result |
| --- | --- | --- | --- |
| 0x1e500 | actual == 0 | 0 | success, writes zero |
| 0x1e500 | actual == 0 | 1, UINT32_MAX | -16, no output write |
| 0x1e700 | unsigned actual > 0 | 1, 0x80000000, UINT32_MAX | success, writes actual |
| 0x1e700 | unsigned actual > 0 | 0 | -16, no output write |

The original output handler resolves entry 8 and emits exactly one four-byte
write through the original DMA register path. A 32-byte poisoned output
allocation proves the other **28 bytes remain unchanged** by these blocks.
There is no evidence here for eight meaningful result words. Both blocks write
the same first word, so it is not a separate immutable result slot per block.
A final first word must be interpreted with the actual execution sequence;
zero/nonzero alone cannot establish that all diagnostic stages succeeded.

Each block first saves the value read at 0x13004 into its local slot and writes
its selector to 0x12004. On success it copies the result, writes the saved
value to 0x12004 and performs the trailing writes `(0x1703c,0)` and
`(0x18038,1)`. On assertion failure, the dispatcher returns -16 immediately,
records that status at its context+48 and skips the output write **and** these
restore/trailing instructions. This is concrete failure-state evidence, not a
completed reset/recovery. The Linux task path must retain state/backing and
require recovery after errors rather than assuming those actions occurred.

The source of register 0x13124, its relationship to the positive/negative
buffers, hardware comparison/CRC semantics, actual branch selection and
physical diagnostic correctness are still unresolved. These software predicates
alone do not define a golden numerical inference result.


## Scheduler record order and zero-extension boundary

The DB blob contains five 64-byte records at offset 256. Their IDs and first
IL slices are observed directly:

| Record ID | DB byte offset | IL address-list index | IL byte offset | Instructions |
| --- | --- | --- | --- | --- |
| 0 | 256 | 1 | 0x1e200 | 18 |
| 1 | 320 | 1 | 0x1e400 | 1 |
| 2 | 384 | 1 | 0x1e500 | 7 |
| 3 | 448 | 1 | 0x1e600 | 1 |
| 4 | 512 | 1 | 0x1e700 | 7 |

Thus the result blocks studied above are referenced by records 2 and 4. This
establishes their table linkage, not that a completed normal execution has
reached them. The existing full nine-buffer fixture still stops after fetching
record 0 and its configuration data, at engine wait return 0x101424.

`scripts/test_diagnostic_scheduler_boundary.py` now checks the limitation more
directly. It builds the real C fenced descriptor, starts the original firmware
from reset/trap and runs the full existing fixture to that wait. Its original
network fetch reads **256 bytes**, although task0-addr0 supplies only **128
initialized bytes**. The fixture intentionally supplies zero padding for the
rest. Original firmware uses the same local network buffer at 0x185740, and
its engine IRQ handler reads offset/mask words at **+0xcc/+0xd0/+0xd4/+0xd8**.
Those four words all originate in that synthetic zero extension.

Two separate cases inject modeled firmware IRQ bits 0x400 and 0x800 through the
**original trap**, rather than directly calling an engine handler or changing
firmware state counters. Both enter original handler 0x1025e6, clear the pending
IRQ, return through original mret and resume at 0x101424. Both return to the
same wait with engine state 1, five remaining records, an active queue, an
unchanged fence and no completion/error mailbox beyond initial 0x702. No code
or counter is patched. Injecting a notification alone therefore does not make
this fixture a complete executable diagnostic. The zero masks prevent those
modeled reads from reporting the expected completion condition; other missing
conditions have not been excluded.

This is **not** evidence that the real Windows diagnostic is malformed. Runtime
network materialization/patching, format/version compatibility, or a difference
between the opaque SROOT executable and the raw research image remain possible
explanations. A guessed register/mask suffix must not be silently appended to
claim successful inference. The next reverse-engineering work must identify
that missing contract or qualify the actual SROOT-loaded executable's ABI.
The fixed Linux bypass phase does not rely on these diagnostic network fields;
it still does not establish model execution.

The two IRQ bits in this test are firmware-local modeled sources. They neither
identify GSI 463/800 nor prove physical GIC delivery or hardware completion.
No real IRQ, engine register or boot configuration is changed by this test.
