# Compiler statistics revision 2

The TensorRT 11.4 Windows compiler produces a statistics blob that runtime
617.42 cannot index correctly. The [runtime probe](model-probe.md) executes
the original CRT and regex initialization, then observes the old header check
rejecting statistics byte `+4=2` with status 2. The outer deserializer ignores
this status. Changing that byte to 1 only makes the old parser calculate the
wrong extent, 3,392 bytes instead of 28,736.

The new Linux helper validates the newer layout without rewriting the blob:

- [Public header](../include/n1x_dla_statistics.h)
- [C parser](../driver/n1x_dla_statistics.c)
- [Original-code and C tests](../scripts/test_statistics_v2.py)

It accepts immutable CPU snapshots, returns record counts/offsets/strides,
and checks their bounds. It does not decode timings, counters, engine fields,
execution-state entries or names, collect DMA data, or submit a task. It is
built as a standalone kernel component, not connected to the bootstrap or
observation modules. Hardware statistics and inference remain unverified.

## Original producer evidence

Pinned input `nvdla_compiler.dll`:
`0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d`.

The complete backend probe `inspect_windows_mtl_collector.py` now observes
entry and successful return of original builder **RVA 0xc76488**, in both its
initial-descriptor and final-translation modes. It checks the original output
length and seven header words without changing the generated manifests or
compiler outputs. The isolated test then executes this same original function
with explicit graph/vector inputs. Only its allocation (`0xf03068`), zero-fill
(`0x14e0`) and byte-copy (`0x1200`) boundaries are modeled.

The builder does the following:

1. Checks enable bytes at graph `+0x3d`, `+0x3e`, `+0x3f`. If all are zero it
   returns 0 without allocating output.
2. Counts active graph records and reserves a 64-byte-aligned metadata area:
   `(active_count * 0x1800 + 0x5b) & ~0x3f`. It requires this to equal context
   `+0x1788`; an inconsistent reservation returns 1 before allocation.
3. Sums the lengths of thirteen pointer vectors at context `+0x738 + 24*i`
   for the serialized record count. Separately, it sums thirteen byte counts
   at context `+0x700..+0x730` for the additional data area.
4. Allocates/zeros the combined extent, writes the header, then visits those
   thirteen vectors in order. For each record it writes the next absolute
   data offset into the source record at **+0x1108**, copies **0x1800 bytes**,
   advances the metadata destination by 0x1800 and data offset by **0x400**.

Header writes are at **0xc766d0**, **0xc766d8**, **0xc766dc**; the record copy
is at **0xc7670c**. The code writes these little-endian fields:

| Offset | Width | Meaning established by producer | ReLU value |
| --- | --- | --- | --- |
| `+0x00` | 4 | Fixed header word | `0x00000300` |
| `+0x04` | 4 | Fixed revision word | `2` |
| `+0x08` | 4 | Serialized metadata record count | `4` |
| `+0x0c` | 4 | Metadata record stride | `0x1800` / 6,144 |
| `+0x10` | 4 | First metadata record offset | `0x1c` / 28 |
| `+0x14` | 4 | Per-record data stride | `0x400` / 1,024 |
| `+0x18` | 4 | Data area offset | `0x6040` / 24,640 |

The header words are kept exact; semantic major/minor ordering of their bytes
is not inferred. Metadata names happen to begin each captured record; the
parser treats the whole metadata payload as opaque. Each record's `+0x1108`
is an offset from the beginning of this statistics blob, not a pointer or DMA
address. In the ReLU artifact the four values are `0x6040`, `0x6440`, `0x6840`,
`0x6c40`, and the blob ends at `0x7040`.

With the four captured final metadata records as inputs, the original isolated
builder reproduces the entire checked ReLU statistics blob, SHA-256
`da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3`.
This reconstructs serialization from metadata, not the still-opaque process
that populates every metadata field. The normal backend probe separately
checks production of the pinned complete outputs.

## Validation and limits

With the ignored vendor/compiler artifacts from [the model workflow](model-probe.md):

```sh
venv/bin/python scripts/test_statistics_v2.py
make test-relu
make kernel-check
```

Ten producer cases cover empty/one/two/four/twenty-six records, all three
enable flags, distribution across all thirteen vectors, extra active graph
records, additional data capacity, a deliberately undersized data area,
disabled production and inconsistent reservation. The active-graph count and
serialized count are separate; a larger aligned gap before data is therefore
permitted by the C parser rather than assuming an exact count-based offset.

The original builder trusts its caller's data-byte counts. A synthetic
inconsistent context with four records but only three data slots still
produces output; the C parser rejects it because the last slot is outside
the supplied extent. This tests independent bounds checking, not a claim
that the normal compiler produces such a blob.

The parser additionally rejects 23 cases covering truncation, unsupported
header/revision/strides, overflowing counts, overlapping or unaligned regions,
duplicate/out-of-bounds record data offsets, null arguments and extents beyond
the format's 32-bit size. Input is deliberately unaligned. Guards and the
input remain unchanged; rejected calls preserve the output structure.

Only the exact observed header and strides are accepted. A successfully
parsed layout does not validate internal record semantics or model execution.
No hardware register writes, driver binding or firmware upload occur.
