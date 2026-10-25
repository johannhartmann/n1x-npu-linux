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

## Runtime selector provenance

`scripts/inspect_runtime_selector.py` executes the pinned runtime constructor
prefix at RVA 0xb910 through 0xb998, with its mutex helper modeled. That prefix
sets context+0x98 to **0x7fffffff** and installs vtable 0x653d88. Its slot +0x208
points to the original three-byte getter at 0x255d0. This executes only a
constructor prefix, not device initialization.

The original direct getter at 0x22788 returns selectors 0 through 3 unchanged
and returns 0x7fffffff for the other tested inputs. The three-byte getter and
diagnostic append give the following observed results:

| Selector | Three-byte getter status | Output bytes | Diagnostic append status |
| --- | --- | --- | --- |
| 0 | 0 | 1, 0, 0 | 2 |
| 1 | 0 | 2, 0, 0 | 0 |
| 2 | 15 | 0, 0, 0 | 2 |
| 3 | 0 | 3, 0, 0 | 2 |
| 42, 0x7fffffff, 0xffffffff | 15 | 0, 0, 0 | 2 |

The getter preserves the five poisoned bytes after its three-byte output. The
append test models the final model-parser boundary, as described above; status
0 does not establish device execution. No enum, version or hardware meaning is
assigned to these byte values. In particular, this selector is not established
as the KMD classifier or HAL profile number. The seven cases use original ARM64 instructions without hardware
access and cannot establish a successful diagnostic or numerical inference.

### Selector write path recovered

The original `init2` implementation at 0x1c150 passes `context+0x98` to
0x756f8 at 0x1c2a8..0x1c2b0, after its device open/setup calls. The resolver
first checks a process-global cache-valid byte at 0x6a5950. If valid, it copies
the DWORD at 0x6a1c30 into the caller's field without validating its range.

On the uncached path, lookup helpers 0x2bb00 and 0x2bfb0 lead to a virtual
provider lookup at slot +0x18, followed by a query at slot +0xb8. The query
receives a pointer to an initially zeroed four-byte local. The resolver reads
only its first byte: **0x23 maps to selector 1; 0x25 and 0x26 map to selector
3**. Every other byte returns status 15 without updating the selector or
marking the cache valid. Success writes both the global cache and the caller's
field, sets cache-valid to 1 and returns 0. Lookup failures return 6 (first
lookup) or 11 (second/provider lookup). The query's returned status is not
checked before interpreting its output byte.

`scripts/inspect_runtime_selector_setter.py` executes the original resolver for
all 256 byte values, six cached values, three lookup failures and one query
error that still supplies byte 0x23: **266 cases**. The helpers, provider and
CFG validation are explicit modeled boundaries; the mapping and cache logic
execute original ARM64 instructions. An exact instruction check ties the
`init2` selector-address argument to this resolver. No model parser or hardware
is invoked. These observations recover the setter but do not identify the
physical provider's byte, its semantic name, or a correspondence with the KMD
classifier/HAL profile. That provider implementation is the next unresolved
link; successful physical diagnostic execution remains unproven.

### Concrete provider getter

The wrapper constructed at 0x2bb68 uses vtable 0x655b80. Its +0x18 slot is
0x2bf60, which returns wrapper+0x10. The wrapper stores the object returned by
0x77208 there. On that function's fresh allocation path, it installs vtable
0x665bc0; its +0xb8 slot is **0x77160**. These are static instruction/table
links; complete map lookup, object creation and device setup are not emulated.

The original getter at 0x77160 takes a backing-record pointer from object+8,
copies **0xc5e8 bytes from backing+8** into a stack buffer, then writes the
DWORD at stack-buffer+0x11c to its caller's output. Its source field is thus
**backing+0x124**. The selector resolver consumes only the low byte of this
DWORD. This getter does not itself issue a kernel call. Where the backing
record is populated remains unresolved: 0x77208 calls 0x79700 for that object;
its fresh allocation path allocates 0xcad0 bytes and calls 0x791e0.

The selector script now has **274 cases**, including eight coupled runs that
replace the synthetic query callback with original getter 0x77160. Only its
stack-probe and exact-length memory-copy helpers are modeled, with a synthetic
backing record. Values 0xabcd0023 and 0xffffff25 exercise ignored high bytes;
0x2300 confirms the byte position. This proves the record-to-selector data
path under those inputs, not the real record contents, its semantic field
name, a Windows kernel response, or physical firmware compatibility.

### Backing record reaches the Windows adapter-query boundary

The fresh path at 0x79700 calls enumeration routine 0x792a8, then constructor
0x791e0. The latter copies record `index` from a process-global vector at
0x6a5ae0 (stride 0xc5e8) to backing+8. It preserves the complete record,
including record+0x11c, which becomes backing+0x124. The constructor writes
its own 0x11a9a marker separately at backing+0.

Enumeration obtains adapter handles, then calls wrapper 0x786b0 with query
types 13 (4 bytes), 15 (4 bytes), 31 (28 bytes), and finally **0 (0xc5b8
bytes)**. The final private buffer starts at stack+0x50; the complete record
starts at stack+0x20. Therefore record+0x11c lies at **private-buffer+0xec**.
The private request starts with two literal header words and initializes a
DWORD at private+0x10 to 1 before querying. This is not evidence for the
remaining private response values or a Linux-compatible ABI.

The wrapper builds an ARM64 structure with handle at +0, type at +4, buffer
pointer at +8, size at +0x10 and zero padding at +0x14, then calls the pointer
at 0x6a5a58. Loader 0x77fe0 establishes table base 0x6a5970; at
0x782d4..0x782ec it resolves the literal `D3DKMTQueryAdapterInfo` through
imported `GetProcAddress` and stores the result at table+0xe8, the same
0x6a5a58 slot. This identifies the dynamic API binding rather than assuming
it from structure resemblance. Microsoft's [query structure documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_queryadapterinfo)
describes these fields; its [query-type documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ne-d3dkmthk-_kmtqueryadapterinfotype)
describes UMDRIVERPRIVATE as vendor-specific UMD/KMD data.

`scripts/inspect_provider_record.py` checks the exact dynamic-binding
instructions and symbol, executes two complete original constructor copies
with synthetic records, and executes four original query-wrapper cases with
captured callback arguments and a propagated error status. Memory helpers and
the OS callback are modeled; enumeration and a real kernel response are not
executed. Static enumeration also filters the vendor field against 0x10de or
0x4144564e, plus capability bits before appending records. Those filters are
observed instructions, not yet an emulated complete enumeration contract.

The recovered chain is now private response+0xec → enumerated record+0x11c →
backing+0x124 → provider DWORD output → low byte → runtime selector. The KMD
producer of private+0xec, its semantic meaning and its physical N1X value
remain to be recovered. No Linux register or protocol setting is derived from
an assumed value here.

### KMD private-field producer candidate

In the pinned `nvlddmkm.sys`, function **0x19dfd58** accepts the private buffer
through query+0x18 and requires query+0x20 to equal exactly 0xc5b8. Null buffers
or a different size return 0xc000000d before writing the private buffer. Its
success prefix writes the header, clears private+0x14 through the buffer end,
sets private+0x10 to 1, invokes another field builder with private+0x20, then
copies **object+0x4648 → private+0xec** and the adjacent DWORD
**object+0x464c → private+0xf0**. At 0x19d9a28 the local dispatcher reads a
query type and uses the table at 0x19d9cc4. Table entry 0 targets 0x19d9a8c,
whose call at 0x19d9a94 invokes this producer.

`scripts/inspect_kmd_private_selector.py` executes six original producer
prefixes through 0x19dfe04, including poisoned/high-byte source words, and four
complete rejection paths (three sizes and a null buffer). The zeroing helper
and other-field builder are modeled. Successful runs stop after the two field
stores; they do not execute the later producer calls or prove that a final
response cannot subsequently change those fields. Static instruction and
table checks establish the local type-0 edge.

This identifies a matching KMD producer candidate. It does **not** yet connect
the physical N1X adapter's OS callback to this dispatcher, identify the source
of object+0x4648, or show its physical value. Other 0xc5b8-size query producers
exist in this driver (including 0x1a7b348), so size matching alone is not
sufficient to assign a hardware class. No numerical architecture/chip name is
assigned to 0x23/0x25/0x26 from this evidence.
