# N1X convolution compiler bridge

The offline bridge now produces a second complete NVDA artifact: an FP16
1x1 convolution with per-channel bias. It uses original native frontend graph
construction, original Windows pretranslation/translation and original native
serialization. It is not a general ONNX compiler or a physical inference
result. The native loader now materializes its global DRAM constants, checked
against the original Windows allocation/content path. The actual Linux ioctl
serializer and original firmware scheduler accept its task offline; engine
arithmetic and physical execution remain unverified.

## Fixture and recovered API

Input/output shapes are `1x32x7x7`: 32 output channels, 1x1 kernel, unit
stride/dilation, zero padding, one group and channel bias. Output channel `i`
has diagonal kernel coefficient `(i % 3 + 1) / 4`, coefficient `-0.125` on
input channel `(i + 1) % 32`, and zero elsewhere. Bias is `i / 32 - 0.5`.
All supplied coefficients are exactly representable in FP16.

Native `NvDlaNetworkAddConvolution` at ELF offset `0x633434` takes network,
input tensor, output count, padding value, five eight-byte `Dims2` values,
two 24-byte `Weights` structs, bias-mode byte, group count and output handle.
The dimensions are kernel, top-left padding, bottom-right padding, stride
and dilation. The ARM64 wrapper receives the weight structs indirectly.
Their fields are type byte at 0, pointer at 8 and signed 64-bit count at 16.
This agrees with NVIDIA's public [INetwork declaration](https://github.com/nvdla/sw/blob/master/umd/core/include/nvdla/INetwork.h)
and [C types](https://github.com/nvdla/sw/blob/master/umd/core/include/nvdla/c/NvDlaType.h);
the modern binary's disassembly and executed getters establish this fixture.

The constructor normalizes FP16 constants into FP32 graph weights. Original
getters must return all 1,024 kernel coefficients and 32 bias values exactly,
with the correct counts. Original tensor getters verify output dimensions.

Pinned native compiler SHA-256:
`28668e74ced3c04ca34f30d41b7ea1d39cabe791418fbad31fcfb009d5558ad9`.
Pinned Windows compiler SHA-256:
`0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d`.

## Constant relocation and backend execution

The frontend emits two operation records. Intermediate kinds 1, 2 and 3
are 8,752, 472 and 69,632 bytes; kinds 0 and 4 remain 44 and 1,368 bytes.
Kind 3 contains process-local constant pointers at `0x258` and `0x8960`.
The capture saves their 2,048-byte and 128-byte payloads separately. Only
these pointer fields are zeroed for cross-process hash checks; all other
bytes and both constant payloads are pinned. The Windows probe relocates
the pointers into its own emulator allocations.

Original MTL constructor `0x617a70` and pretranslation `0x624630` execute
using the existing Windows/CRT harness. That harness first runs its existing
ReLU collector/dispatch checks and original CRT/C++ initializers, then calls
the convolution probe. It supplies no ReLU translation result to convolution.
Additional environment queries `HBC_CRP_MODE`, `FLUSH_ATTENTION` and
`DLA_DISABLE_FCDMA_PORT1` are explicitly modeled as absent.

The pretranslation result is replayed into a new native process, restoring
its own live constant pointers. At the second handoff the frontend supplies
additional constant-copy pointers at `0x2e0` and `0x89e8`; their payloads must
match the captured constants. Both pairs are relocated for Windows translation.
All other frontend edits are checked against the observed field changes.

Original translation `0x624eb0` returns zero and retains unchanged inputs.
Original size/copy getters verify every output, destination guards and the
empty-output error status. The native serializer consumes those exact outputs
through the existing audited no-output imatrix adapter. Instruction blobs and
numerical results are not synthesized.

## Artifact and runtime boundary

The artifact is 2,817,056 bytes, SHA-256
`717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b`.
It has one task, ten memory/address entries and task-local address IDs
`[4, 2, 1, 3, 5, 6, 7, 8, 9]`.

| Content | Bytes |
| --- | ---: |
| Instructions | 44,544 |
| Control / network | 320 / 256 |
| Preemption context | 2,738,944 |
| Statistics | 28,736 |
| Kernel / bias constants | 2,048 / 128 |

Compiler debug output describes four spatial tiles, FP16 kernel storage and
FP19 bias storage. Constants use task-local address slot 2 at offsets 0 and
4,096. The memory table places them in one 8,192-byte initialized DRAM
allocation. The global DRAM pool property is 4,224 bytes; local DRAM/CVSRAM
properties are zero. These are producer observations, not a general runtime
pool contract.

`verify_conv_loadable.py` checks blobs against backend output and captured
constants, plus shapes, formats and address mapping. The native image is
2,850,816 bytes, SHA-256
`7ca5a9d72dd1608e5f9aa0f746d02b4de7906424d68bba0e22f52ff356b181db`.
Changing either input bytes or constant contents works without an artifact
hash allowlist. Ten malformed/unsupported pool, extent and task-address
variants are rejected.

## Original runtime allocation evidence

`inspect_conv_memory_allocation.py` executes the hash-pinned 617.42 Windows
runtime's constructor and complete deserializer on the convolution artifact.
Original loadable getters at `0x8bb60`, `0x8bd30` and `0x8b8c0` return local
CVSRAM=0, local DRAM=0 and global DRAM=4,224. Their vtable slots are `0x90`,
`0x98` and `0xa0`. The global getter delegates to property lookup `0x8c260`,
which reads an eight-byte payload from the content map.

The original per-memory path `0x1ec70` requests the descriptor's size through
provider boundary `0x74798`, stores its returned handle/CPU pointer, calls
boundaries `0x76c58` and `0x74e78`, and enters the full content loader
`0x20d00`. For memory 1 it requests **8,192 bytes**, then copies 2,048 bytes
at offset 0 and 128 bytes at offset 4,096. It does not invoke any of the pool
getters during this tested allocation path or request another 4,224-byte buffer.
Every initialized memory is compared with the native loader's materialization;
unused native bytes are zero while unused modeled Windows bytes retain poison.

Eighteen cases cover all ten memory records, exact 4,224-byte capacity,
one-byte-short and second-offset-at-end destinations, an existing handle,
a size exceeding `UINT32_MAX`, and failure at each of the three provider calls.
Short destinations return status 6 after preserving the earlier kernel-constant
copy. Provider failures prevent subsequent provider calls and content copies.
The existing-handle branch skips allocation and still initializes content.

This executes original allocation decisions and copy loops, **not a real
Windows allocation or full model-load orchestration**. Runtime/device objects
are RAM fixtures; the device getter and three provider boundaries are modeled.
The optional allocation cache is disabled. The test proves neither physical
memory reuse safety nor reset/teardown. Output is written locally to
`results/conv-memory-allocation.json`.

For this observed layout, the native loader requires one initialized,
non-I/O, raw-constant allocation, a global property equal to the highest
initialized end offset, and a task address covering that extent. This is an
explicit supported-layout check, not a claim that the Windows runtime enforces
the same equality for every model. Nonzero local pools and other global-pool
layouts remain unsupported. The property does not create a second allocation.

`test_linux_accel.py --conv` sends the prepared image through the actual kernel
ioctl serializer (with modeled Linux/DMA boundaries), then the unmodified
HAL-1 firmware reset/trap/scheduler. All nine task addresses are serialized
correctly and the eight expected engine address pairs are programmed. Firmware
waits for the engine; no convolution arithmetic or output accuracy is modeled.

## Independent numerical reference and hardware acceptance

`runtime/run_conv.py` verifies all 1,024 kernel values and all 32 bias words
against the source graph before accepting the pinned loadable. The kernel is
row-major FP16 for this fixture. Each bias occupies a u32 whose low 19 bits
contain sign/exponent/fraction; shifting the word left by 13 produces the
source FP32 bit pattern. For example, `0x5f800 << 13` is FP32 `-0.5`.
Every fixture bias is exactly representable. Physical interpretation and
arithmetic-engine rounding/exception behavior still require hardware tests.

The named native functions `nvdla::priv::fp19_to_fp32` at ELF offset `0x4e1d20`
and `fp32_to_fp19` at `0x4e1d80` provided the disassembly reference for finding
the Windows counterparts at RVAs `0x3d93b8` and `0x3d9420` in the pinned 11.4
compiler. `scripts/inspect_fp19_conversion.py` executes both implementations'
unmodified leaf instructions in Unicorn with no helper models. All 1,056
encoding and 1,040 decoding cases agree with an independent integer reference,
including signed zeros, subnormals, rounding ties, exponent carry, maximum
finite values, infinities and NaNs. All 32 actual bias words also encode and
decode exactly through both original binaries.

For normal finite inputs the software encoder rounds to nearest with ties to
even. It flushes FP32 subnormals to signed zero and clamps overflow and infinity
to signed maximum finite FP19 (`0x3fbff` plus sign). NaNs encode as `0x3ffff`.
The decoder preserves encoded infinity and maps NaNs to FP32 bits `0x7f8003ff`;
for other low-19-bit words, the result is a left shift by 13. Consequently,
the shift shortcut is valid for the finite fixture biases but not every special
value, and encode/decode is not universally a lossless round trip. These are
software-function observations, not a claim about engine arithmetic or a trace
of the complete constant-producing call chain.

`scripts/create_conv_probe.py` produces an independent ONNX Conv graph and
three changing input patterns in `results/conv-probe/`. ONNX ReferenceEvaluator
executes the full 32-channel convolution, while the acceptance CLI computes
the source graph using exact integer arithmetic. Packing is checked separately
with vectorized NCHW/CHW16 transforms. All 1,568 outputs of each case agree
byte-for-byte; all three expected outputs differ.

Inputs are odd multiples of 1/16 between -31/16 and 31/16. For channel `c`,
with input numerators `n`, the exact output in units of 1/128 is
`2*(c%3+1)*n[c] - n[(c+1)%32] + 4*c - 64`. These outputs and intermediates
are exactly representable, finite and normal; outputs cannot be zero. This
tests bias, channel mixing, the channel-31 wrap and spatial/tensor placement
without hiding mismatches behind a tolerance. It is not general convolution
accuracy or performance qualification.

After the separate [native accel startup and ReLU qualification](native-runtime.md#first-hardware-qualification-not-yet-performed),
use the actual `n1x-dla` node (the example assumes `accel0`):

```sh
venv/bin/python scripts/create_conv_probe.py
make runtime
pkexec /usr/bin/python3 "$PWD/runtime/run_conv.py" \
  --model "$PWD/results/native-compile-t254-conv1x1/conv1x1.nvda" \
  --device /dev/accel/accel0 \
  --input "$PWD/results/conv-probe/channels-input.fp16" \
          "$PWD/results/conv-probe/spatial-input.fp16" \
          "$PWD/results/conv-probe/negative_channels-input.fp16" \
  --output-dir "$PWD/results/NEW-accel-conv"
```

The runner validates every input before opening the device and checks the
retained-job budget before submission. Each output starts as a qNaN pattern.
Only `Device.read` supplies the actual output; CPU values are comparison data.
Acceptance requires an exact output match, unchanged input and unchanged full
constant allocation. Attempt/job/error records and raw outputs are flushed
to disk. Any failed comparison or I/O error stops the sequence without retry.
This retains the existing kernel memory-lifetime restrictions.

`scripts/test_conv_acceptance.py` checks nine modeled-device scenarios:
three successful changing inputs, untouched poison, incorrect output, a stale
second result, changed input, changed constants, submit timeout, read failure
and insufficient retained budget. Seven invalid-input cases and a later
invalid input verify rejection before device opening. These are CLI acceptance
tests with modeled reads, not physical convolution results. The hardware run
above remains pending; the current boot still has the old module without accel.

## Reproduce

With the ignored vendor binaries and existing ReLU capture prerequisites:

```sh
make test-conv
```

Native code runs in the existing device-free bubblewrap sandbox with private
`/dev`, absent `/sys`, isolated namespaces and a 120-second alarm. Windows
code runs only in Unicorn with modeled OS/CRT boundaries. Artifacts are under
`results/native-compile-t254-conv1x1/`, `results/windows-conv1x1-pretranslation/`
and `results/windows-conv1x1-backend/`; ReLU directories are preserved.
Vendor binaries and generated loadables remain ignored and unpublished.

Numerical output, physical execution, runtime pool handling and broader graph
coverage remain unqualified. No hardware, module or boot configuration changes
are performed by this pipeline.
