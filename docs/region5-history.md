# Region-5 recent-history reader

The Windows reader at RVA `0xd465a0` copies recent history from the region-5
ring. **It does not consume entries or advance either cursor.** The C helper
`n1x_dla_region5_history` reproduces that view for an immutable CPU snapshot.
It is not connected to live Linux DMA storage or to the bootstrap module.

## Original Windows algorithm

`test_region5_history.py` executes the original Windows reader and provider
`0xd470d0` against the SHA-pinned NVIDIA 617.42 KMD. The provider's actual
stride callback `0x61a7f0` returns 52. The OS getter, cache boundary and bounded
memory-copy helper are explicit stubs; the reader/provider arithmetic executes
as original ARM64 code.

The original cache helper receives the whole 1-MiB allocation and argument 0
before the read. The ring itself starts at allocation +16 and contains two
little-endian u32 byte offsets followed by 52-byte opaque records. Subtracting
8 and dividing by 52 gives the first and second indices; `(ring_size-8)/52`
gives the capacity. Their producer/consumer ownership and update ordering
have not been established here, so they are not labeled read/write indices.

The observed recent-history selection is:

1. Equal indices: return without changing output, including the old count.
2. First index zero: select at most 50 records preceding the second index.
3. First index nonzero: select 50 records, even when the cursor distance is less
   than 50. This is history, not a count of newly available entries.
4. Copy that suffix in ring-index order, wrapping into a second span
   when needed, and store the selected count in the output's first u32.

The observed initial zero/zero host header also returns without copying in
Windows. It is a valid empty initial state: registration normalizes only the
firmware-local header, not the host allocation. The C helper explicitly accepts
this pair and reports count zero; a single zero cursor remains invalid.

The production ring's provider-derived capacity is 20,164, so a 50-record
history fits. Original Windows byte offsets equal to the ring size are accepted
and handled by the subsequent modulo arithmetic. Identical raw indices are the
empty test; equivalence of differently encoded physical positions is not
inferred. Neither header nor ring memory is changed by the reader.

The separate last-record query at `0xd46450` checks that the two header offsets
differ and the second is greater than 52. It inspects the preceding 52-byte
record only if its first byte is 1, then returns raw byte **2**, followed by raw
byte **1**, through two u32 output pointers. Tests prove the ordering and the
nonmatching-tag zero outputs. They do not establish names, units or meanings
for those bytes or for the remaining 49 bytes. No structured-record decoder or
completion inference is added from this observation.

## C snapshot contract

`driver/n1x_dla_ring.c` accepts an immutable CPU ring snapshot, source extent,
separate destination/capacity and separate count pointer. It returns the same
nonempty history data as Windows. It deliberately sets count to zero for a
valid empty snapshot instead of leaving an old caller value untouched.

It always validates source length, a minimum of 50 records, whole-record
sizing and 32-bit representability. Apart from the zero/zero initial pair,
cursor ranges and 52-byte congruence are checked before copying. Invalid input or a short output preserves both output and count.
An empty snapshot can use a null/zero-capacity output. Nonempty output is at
most 2600 bytes, with at most two bounded copies. Invalid cursors are rejected
rather than copying Windows's less defensive behavior.

All objects must be valid separate CPU memory. **Do not pass a live DMA ring**
and assume this validates atomicity: a future backend must first establish
coherency, producer publication order, cursor ownership and a stable capture.
The helper neither issues DMA barriers nor modifies producer-owned storage.
It does not establish task completion or DMA quiescence, and is not an
inference-result parser.

## Reproduction and evidence

```sh
venv/bin/python scripts/test_region5_history.py
make kernel-check
```

The test compares original Windows and C output for 13 indexed empty/linear/wrapped/
end-position cases, including untouched output canaries and source bytes, plus the initial
zero/zero host header. It
also checks the two last-record tags, 13 invalid extent/header/output-capacity
cases, null-pointer controls, empty null-output behavior and the C minimum-
capacity wrap boundary. All addresses, cache effects and contents are synthetic.

The component compiles against both current and isolated test-kernel headers.
The source/object manifest for the target check is in
`results/npu1-region5-history-build/`. No module is loaded or changed by these
tests and no live ring is read. Firmware record production, semantic fields,
publication/coherency on hardware, full initialization and inference remain
unverified. See [region-5 setup](region5.md) for the separate registration phase.
