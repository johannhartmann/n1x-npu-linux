# N1X convolution compiler bridge

The offline bridge now produces a second complete NVDA artifact: an FP16
1x1 convolution with per-channel bias. It uses original native frontend graph
construction, original Windows pretranslation/translation and original native
serialization. It is not a general ONNX compiler or a physical inference
result. The native loader currently rejects its nonzero global DRAM pool.

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
constants, plus shapes, formats and address mapping. It explicitly verifies
that the current native loader rejects the nonzero pool property. Enabling
this model requires investigating the original runtime's pool interpretation;
silently ignoring that property is not a supported workaround.

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
