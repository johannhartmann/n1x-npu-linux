# Raw HAL-1 Region-5 producer and publication

The raw HAL-1 producer at `0x106ab6` is now exercised with original reset,
registration and DMA functions. **This is not a physical ring test.** The
producer is invoked explicitly with a synthetic caller record; its surrounding
caller and record semantics are not established by this test. The opaque image
loaded through SROOT is still not proven byte-identical to this raw image.

## Method and scope

`test_region5_producer.py` imports the 1-MiB descriptor/ring through actual
command 11 after original reset, trap and ACK. It retains that same emulator
instance and its DMA register model, then calls `0x106ab6` with a 52-byte source
in explicitly synthetic caller-owned mapped RAM. No firmware function is
stubbed or instruction changed. The test observes every issued DMA, host/local
headers, full allocation changes and C recent-history output.

The code uses the descriptor at local `0x1859d0` and the cached two-word header
at `0x1860f8`. Zero region address or zero region length skips all DMA. The return
register on that early exit is not assigned a status interpretation; it may
still contain the input record pointer.

## Normal publication and collision

For a nonzero configured region:

- The candidate second cursor is its old value plus 52.
- On the non-wrap path, data goes to region base plus the old second cursor.
- If the candidate cursor equals the cached first cursor, the first cursor is
  advanced by another 52, wrapping to 8 at the size boundary.
- The producer issues all 52 record bytes through the actual DMA helper.
- Only after that helper reports completion does it update the cached second
  cursor and issue the full eight-byte header to the region base.

Both header fields are therefore firmware-written in this path. They are not
host-consumed cursor storage; neither tested Windows reader advances them.
The fixture `(8,8)` is the cached empty state. The host can still contain its
initial zero/zero header until the first publication changes it to `(8,60)`.

The model may split a record into multiple DMA commands: the initial aligned
case uses six eight-byte writes plus a four-byte write, followed by the
separate eight-byte header. The test checks total extent and exact sequential
source/destination ranges, not an assumption that 52 bytes is a single transfer.
All non-header allocation bytes outside the chosen record remain unchanged.

A completion-status negative control holds status `0x118` bit 1 clear after
record commands have issued. This model copies data synchronously on command
issue, so the full record is already visible in modeled RAM, but the original
helper remains in its completion poll and **no header DMA is issued**. Releasing
the status resumes that same pending firmware state; it does not restart the
producer or issue a second record. The subsequent publication completes.

This proves the observed software ordering under explicit device effects. It
does not prove real DMA visibility, coherence, barriers or atomic publication.
In particular, one modeled eight-byte header command is not evidence of an
atomic eight-byte update as observed by the physical CPU.

## Wrap-path quirk in the raw reference image

With the second cursor at `ring_size-52`, the candidate equals the region size.
The original branch does **both** of the following:

1. Writes the new record at region offset 8, rather than at the old second
   cursor's final slot.
2. Publishes the second cursor as 8.

For the fixture with first cursor 8, the published pair is `(60,8)`. The C
history routine, independently matched to the Windows reader, does not include
that newly written slot in its recent-history view. A second producer call
writes the same slot again, publishes `(112,60)`, and makes the second record
visible. The first wrap-path record has been overwritten without appearing in
that view. Full allocation comparisons verify both operations.

This is an observed **raw-image quirk**, not a claim that the physical P16's
opaque SROOT firmware has a demonstrated bug. Its intent and applicability to
the actually loaded image are unresolved. The host history reader is not
silently changed to compensate, nor is vendor firmware patched. Do not use this
history as a lossless event stream, a produced-record counter or a task/inference
completion authority.

## What was tested

```sh
venv/bin/python scripts/test_region5_producer.py
```

Six scenarios cover initial zero host header, linear publication, second-cursor
wrap, collision/first-cursor advancement, first-cursor wrap, and blocked DMA
completion followed by resumption of the same state. The wrap case includes a
second publication proving overwrite. Two additional controls independently
remove the region address or length and verify that no DMA is issued.

The test checks unchanged firmware bytes, actual DMA function execution, data
before header, all source/destination extents, exact host/local header changes,
untouched allocation bytes and output from the actual C history helper. The
producer has one observed direct call at `0x1071e0` in surrounding function
`0x106b4e`; that caller is not executed here, so timing, activation conditions
and construction/semantics of real records remain open.

A stable live CPU capture, opaque-firmware identity, physical behavior, full
initialization and numerical inference remain unverified. See
[history selection](region5-history.md) and [buffer setup](region5.md) for the
separate host-reader and registration evidence.
