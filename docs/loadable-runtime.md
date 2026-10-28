# Native NVDA loadable ingestion

`runtime/loadable.py` now reads supported compiler-produced NVDA files directly,
materializes private model memory, resolves the task-local address list and
exposes named tensor metadata. `runtime/run_loadable.py` connects that loader
to the native C/Python accel client. It does not require the preassembled ReLU
template and does not contain a loadable-hash allowlist.

This implements one more userspace layer of the Linux driver. Compilation of
arbitrary ONNX graphs, broader model qualification and physical execution via
the accel API remain open. Kernel instructions, firmware and boot artifacts
were not changed for this step; the previously prepared accel module remains
the pending hardware-test build.

## Newly traced modern fields

The old public `loadable.fbs` alone is insufficient for the current artifact.
Two inline three-byte fields were missing from our earlier projection:

| Field | Original Windows behavior | Pinned artifact |
| --- | --- | --- |
| Root field 9 | Reads at RVAs `0x88aac`, `0x88ab0`, `0x88ab4`; copied to model context `+0x1a8` | `3.0.0` |
| Blob field 6 | Reads at RVAs `0x88468`, `0x8846c`, `0x88470`; copied to content node `+0x6b` | `3.16.0` for DLA blobs, `0.0.0` for properties |

Radare2 disassembly identified these copies; the original ARM64 deserializer
then verified them under Unicorn. The runtime binary and baseline loadable are
hash-pinned by `inspect_loadable_extensions.py`. Five cases run the complete
existing deserialization/materialization path:

- Unmodified artifact: both fields appear at their expected destination offsets.
- Root version changed to `4.5.6`: overall deserialization still returns zero,
  but statistics processing returns status 2 before reading its header.
- Root field omitted: this artifact produces context version `0.0.0`, with the
  same statistics-header rejection. It does not inherit `3.0.0` automatically.
- Blob auxiliary versions changed to `55 aa 33`: the new bytes survive into all
  eight content nodes; deserialization completes.
- Blob auxiliary fields omitted: all eight content nodes receive the original
  runtime's default `3.13.0`.

The auxiliary field is version-shaped metadata, plausibly associated with the
compiler, but its complete downstream meaning has not been established. No
claim is made that changing it creates firmware compatibility. The native
loader requires the observed explicit versions; it does not silently apply
the legacy defaults or discard these fields.

Local evidence: `results/loadable-extensions.json`,
`results/runtime-root-deserializer.asm`, `results/runtime-blob-materializer.asm`.
The original deserializer regression still passes, including its existing
malformed-reference and unsafe-statistics-version controls.

## Supported ingestion contract

The loader currently accepts:

- Serialization `0.9.0`, root interface version `3.0.0`.
- DLA contents with interface 1, content version `3.0.0` and auxiliary version
  `3.16.0`; raw interface-0 contents require explicit zero versions.
- One DLA task on the default/first instance, one submit, no pre-waits or
  relocations, and one terminal event `{id=0,type=0,target=0,value=1,op=SIGNAL}`.
  The kernel's unique fence implements that single terminal completion.
- DRAM allocations, ordered dense memory/address IDs, allocation/content/input/
  output flags and power-of-two alignment up to 4096. Local pool properties
  must be zero. A nonzero global DRAM property is supported for the checked
  single initialized raw-constant allocation described below.
- Static batch-1 tensor descriptions with raw format codes, shape, strides,
  logical byte counts and checked memory/binding references.

The one-task restriction does not mean one neural-network operation: one
compiled task may contain a larger graph. Its arithmetic and target
compatibility must still be qualified separately. Multi-task/event chains,
relocations, SRAM and additional pool layouts need implementation before those
loadables can run correctly.

Bounds are checked against both the FlatBuffers object extent and the file.
Inline versions must fit inside their owning object. Vtables, forward offsets,
vectors, UTF-8 names, IDs, duplicate names/bindings, content extents, tensor
extents and task references are checked before materialization. Unknown table
extensions are rejected. Resource limits are 64 MiB per file, 32 MiB prepared
image, 4096 entries per metadata vector, 65536 traversed metadata entries and
1 MiB cumulative names. Payload byte vectors have a separate 32 MiB limit.

Each model memory gets at least 256-byte backing and cache-read padding, while
its logical size remains distinct. Named content copies retain their specified
order. Input bytes are supplied by tensor name, with exact byte counts, into a
fresh image for every submission. Format enum value 3 in the current tensors
is preserved; it is not mislabeled using the old public NCHW/NHWC-only enum.
No implicit layout conversion, quantization or CPU inference occurs.

Instruction streams remain trusted. Parsing a structurally valid loadable is
not proof that its embedded register/DMA commands are safe or that it targets
this hardware. The kernel's RAWIO requirement and retained-memory policy still
apply; see [native-runtime.md](native-runtime.md).

## Usage

Inspection is read-only and needs only Python's standard library:

```sh
python3 runtime/loadable.py results/native-compile-t254/relu.nvda
```

After a separately qualified accel startup, the native runner accepts named,
already-packed inputs:

```sh
make runtime
pkexec /usr/bin/python3 "$PWD/runtime/run_loadable.py" \
  --model "$PWD/results/native-compile-t254/relu.nvda" \
  --device /dev/accel/accel0 \
  --input "input=$PWD/results/accel-qualification-inputs/input-a.fp16" \
  --output-dir "$PWD/results/NEW-loadable-run"
```

The runner validates the model and all inputs before opening the device. It
writes/fsyncs model and attempt records, records the returned job ID, then reads
named outputs using their declared image extents. Output filenames are numeric;
model-controlled tensor names never become paths. Failed execution produces an
error record and no automatic retry. A successful result says completion was
observed and explicitly leaves numerical accuracy unverified.

## Verification and limits

`test_native_loadable.py` compares the entire 2,830,336-byte model image with
both the independent pinned-fixture builder and the existing native C content
initializer, which was checked against original Windows initialization.
Two different inputs preserve all non-input bytes. Renaming both tensors works
without a hash allowlist. Forty-three malformed/unsupported loadables and four
invalid input mappings are rejected. Modeled CLI tests verify output transport,
durable records and error/no-retry behavior; they do not calculate a device
result with a CPU oracle.

`test_linux_accel.py` now feeds **both** the old template and the new loadable
layout through the exact kernel ioctl serializer and the original HAL-1
reset/trap/scheduler. Both are accepted and program the expected engine addresses.
The new layout includes the model's otherwise unused allocation 0, so task
addresses differ from the old template. This test verifies relocation through
the address list; engine arithmetic remains unmodeled.

```sh
venv/bin/python scripts/inspect_loadable_extensions.py
venv/bin/python scripts/test_native_loadable.py
venv/bin/python scripts/test_linux_accel.py
```

No new physical inference result is claimed. The remaining hardware test must
compare actual NPU-written outputs with an independent reference, including
multiple jobs and the new loadable-generated layout.

The separate [convolution compiler fixture](convolution-compiler.md) now
produces a complete loadable with two constants in an 8-KiB allocation and a
4,224-byte global DRAM pool property. Original Windows runtime execution now
confirms that the tested allocation path uses the memory-table size and copies
both constants at their listed offsets; the property needs no second backing.
The loader accepts this layout when one initialized non-I/O allocation contains
only raw constants, their highest end equals the property, and a task address
covers that extent. This is our qualified layout restriction, not a universal
Windows validation rule. The remaining local pools must be zero.

The resulting 2,850,816-byte image matches every original content copy.
`verify_conv_loadable.py` checks changed inputs/constants and ten invalid pool
variants; `inspect_conv_memory_allocation.py` exercises eighteen original
allocation/content cases with explicit provider models. The extended
`test_linux_accel.py --conv` verifies serialization and original-firmware
scheduling. No physical convolution result is claimed.
