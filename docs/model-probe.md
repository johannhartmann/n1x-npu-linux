# A numerical probe for the Windows ARM DLA compiler

This is a reproducible source model and CPU oracle, **not a compiled DLA
loadable or a working Linux inference path**. It replaces neither firmware
qualification nor the driver work. The legacy built-in diagnostic is gated
away for the reconstructed P16 runtime selector; see [the diagnostic audit](diagnostic-contract.md).

## Compiler route verified on 2026-10-25

NVIDIA's [TensorRT 11.4 documentation](https://docs.nvidia.com/deeplearning/tensorrt/latest/)
introduces Windows ARM support for DLA-capable RTX Spark laptops. The
[platform breakdown](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/work-with-dla.html)
states that DLA is beta on Windows ARM and unsupported on Linux SBSA in this
release. This provides a new compiler investigation route; it does not add
Linux NPU support or establish compatibility with our pinned driver 617.42.

The [Windows ARM layer specification](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-windows-on-arm.html)
lists FP16 ReLU with CHW shape `[32,7,7]` as validated, requires fixed dimensions
and batch 1, and provides FP16 CHW16 input/output formats. GPU fallback is
unavailable. The probe uses precisely that small activation shape and format.
Keep the ONNX source: NVIDIA requires rebuilding beta engines for general
availability. Compiler acceptance and numerical correctness still need testing.

## Generate the source and independent reference

```sh
venv/bin/python -m pip install -r requirements-probe.txt
make generate-probe
```

The generator writes ignored `results/relu-probe/`:

- `relu-fp16.onnx`: one ReLU, shape `[1,32,7,7]`, FP16, ONNX opset 13, IR 8.
- `input-nchw-fp16.bin`, `expected-nchw-fp16.bin`: little-endian logical tensors.
- `input-chw16-fp16.bin`, `expected-chw16-fp16.bin`: packed tensors for compiler I/O.
- `manifest.json`: sizes, hashes, shape, packing and reference package versions.

Each raw tensor is 3136 bytes. Inputs cycle through exactly representable
binary fractions from -2 to 2, including zero. The generator checks ONNX
structure and compares ONNX ReferenceEvaluator output byte-for-byte with
independently computed NumPy ReLU. It also checks the CHW16 packing roundtrip.
No subnormal, NaN or negative-zero behavior is assumed by this oracle.
Execution is CPU-only; a passing generator proves no NPU behavior.

## Candidate compile command (not yet run)

On a compatible Windows ARM TensorRT 11.4 installation, NVIDIA documents the
[standalone loadable workflow](https://docs.nvidia.com/deeplearning/tensorrt/11.4.0/inference-library/dla-standalone-mode.html).
Its `trtexec` form adapted to this model is:

```powershell
trtexec.exe --onnx=relu-fp16.onnx --saveEngine=relu-dla.bin --useDLACore=0 --safe --inputIOFormats=fp16:chw16 --outputIOFormats=fp16:chw16 --skipInference
```

This is an unexecuted candidate command, not a verified build recipe for this
machine. Tensor types provide FP16 precision; the removed `--fp16` flag is
not used. Preserve compiler version, logs, resulting file hash and the source
manifest before interpreting a produced file. Parse and compare its network,
instruction and address structures before allowing any Linux submission.

## Package investigation state

The official [NVIDIA Python package index](https://pypi.nvidia.com/tensorrt-cu13-libs/)
lists `tensorrt_cu13_libs-11.4.0.106-py3-none-win_arm64.whl`, advertised SHA-256
`a3231fed4887bed48c1da0c2f7a1b88ca39a5021e80870ab8b86ab99ce9b7391`.
Its remote ZIP directory was inspected: it contains `nvinfer_11.dll`,
`nvonnxparser_11.dll`, `nvinfer_plugin_11.dll` and a builder-resource DLL. No
separate DLA compiler DLL is listed. The complete download and code inspection
are pending; the archive's advertised hash is not yet a locally verified hash.
Windows binaries are not installed into the Linux Python environment. No
compiler execution, generated DLA binary or physical inference is claimed.

### Compiler dependency and parser audit

The acquired `nvinfer_11.dll` contains the
dynamic library name **`nvdla_compiler.dll`**, the queried symbol
**`NvDlaCompilerGetSemVersion`**, compiler-compatibility error messages and a
large `NvDlaTensor*`/`NvDlaLayer*` API string set. This is string evidence of a
separate compiler dependency, not an executed loader or validated function
binding. The Wheel directory does not list that dependency. Therefore the
Windows wheel alone is not yet a demonstrated DLA compilation environment.

The complete `nvonnxparser_11.dll` was extracted from its already downloaded
ZIP entry. `scripts/inspect_tensorrt_parser.py` pins the acquired ARM64 PE hash:
`ad44a8a9be9ab824540b6363b8cb6e63f0d879b97c27b71ef4fde50c59068207`.
The wheel RECORD instead describes a 2260480-byte file. Removing the trailing
10352-byte certificate table and zeroing only PE checksum/security-directory
fields reconstructs that exact pre-sign size and RECORD SHA-256:
`eb2896c3cfce1c91a2e4619b0296fc13b3a3447d9f7930c73a1aeca2a2990306`.
The acquired signed file is preserved unchanged. This establishes consistency
with the package record, **not certificate-chain trust validation**.

Its imports are KERNEL32 and dbghelp; exports include the parser/refitter
factories and parser-version getter. DLA capability and version-3.16 feature
strings are present. These do not establish compilation or N1X compatibility.

```sh
venv/bin/python scripts/inspect_tensorrt_parser.py
```

NVIDIA's [Debian SBSA package index](https://developer.download.nvidia.com/compute/cuda/repos/debian12/sbsa/)
also publishes `libnvinfer-dla-compiler11_11.4.0.106-1+cuda13.4_arm64.deb`.
It has been acquired as a native analysis counterpart. Its existence
does not override NVIDIA's stated absence of supported Linux SBSA DLA in this
release. Contents and exported ABI are now partly inspected below. Nothing
from this package has been installed into the system. Native library loading
is restricted to the isolated graph-construction probe described below.

### Native compiler counterpart acquired

The complete Debian package SHA-256 is
`9954f3c1d88836767a0657e39e2050c32499b950860524e8e6dc8d0ecc48a2cc`.
Its metadata describes a re-vendored DLA compiler and supplies
`usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so`. The library SHA-256 is
`28668e74ced3c04ca34f30d41b7ea1d39cabe791418fbad31fcfb009d5558ad9`.
It is an AArch64 ELF with only libc, libm, pthread, librt and dynamic-loader
dependencies, with no direct CUDA dependency. This does not rule out dynamic
loading or establish that compilation never queries hardware.

Exported APIs include `NvDlaCreateNetwork`, `NvDlaNetworkAddInput`,
`NvDlaNetworkAddActivation`, `NvDlaNetworkMarkOutput`, wisdom/profile/target
configuration, `NvDlaCompilerCompile` and loadable-image size/data retrieval.
Their network-building ABI still needs reconstruction before use. Profile
exports mention RVV controls; names alone do not establish target capabilities.

The Windows `nvinfer_11.dll` ZIP entry has downloaded completely, although the
larger wheel download continues. Its SHA-256 is
`36b2cf0523259d4602037c120a793739fdd6068bad86d73b596453817c75de3f`.
The analogous pre-sign reconstruction matches its RECORD digest
`f6409653b1c588d496f5dcc4cda1a60dbd310246d199b865342cac4d23c3ef9d`.

`scripts/inspect_dla_compiler.py` executes the native compiler's original leaf
`NvDlaCompilerGetSemVersion` at ELF address 0x6217b0 under Unicorn. It returns
four u64 fields **`{1,3,16,13}`** with status 0, preserves poisoned trailing
storage and returns 4 for a null output. The test then executes the original
Windows comparison slice at RVA 0x725fb4: that tuple equals its required
version; six neighboring tuples verify the lexicographic direction. No library
constructors, dlopen, compiler build or hardware operation execute.

```sh
venv/bin/python scripts/inspect_dla_compiler.py
```

The version match makes this ELF a concrete candidate for studying the C API
and generating the probe without executing Windows TensorRT. It does not
establish target support, full ABI equivalence, successful compilation or a
working Linux NPU driver.

### Native graph-construction API exercised

`scripts/probe_native_graph.py` now loads the pinned compiler library in a
bubblewrap child with isolated namespaces, no network, a private `/dev`, no
`/sys`, read-only source/interpreter mounts and a temporary `/tmp`. There are
no host GPU/NPU device nodes in that child. This is native ARM64 execution,
including library initialization and graph object allocation, unlike the leaf
version emulation above. It does not invoke `NvDlaCompilerCompile`.

```sh
venv/bin/python scripts/probe_native_graph.py
```

Recovered C signatures used by the probe, with `H` denoting an opaque pointer:

| Function | Arguments | Result |
| --- | --- | --- |
| NvDlaCreateNetwork | H* output | u32 status |
| NvDlaNetworkAddInput | H network, char* name, 16-byte dimension struct by value, H* output | u32 status |
| NvDlaTensorGetDimensions | H tensor, dimension-struct* output | u32 status |
| NvDlaNetworkAddActivation | H network, H input, u8 activation code, H* layer | u32 status |
| NvDlaLayerGetOutput | H layer, i32 index, H* tensor | u32 status |
| NvDlaNetworkMarkOutput | H network, H tensor | u32 status |
| NvDlaNetworkGetNumInputs / Outputs | H network, u32* count | u32 status |

The 16-byte dimension argument occupies two ARM64 argument registers, matching
four 32-bit fields. The probe supplies `{1,32,7,7}`, creates an activation with
numeric code 0, retrieves its output and marks it as the network output. It
checks all statuses, non-null handles, one input/one output, and exact input
and output dimension roundtrips, then destroys the network. The public
[NVDLA C type header](https://github.com/nvdla/sw/blob/master/umd/core/include/nvdla/c/NvDlaType.h)
provides the reference definitions `RELU=0`, `HALF=2` and dimension order
`n,c,h,w`. The probe now also checks activation-code retrieval, sets both
tensor types to 2, and verifies their getter roundtrips. Those names come
from this C API counterpart, rather than TensorRT's different enum. No tensor
data is evaluated; target/profile/compile setup and numerical validation remain
open, and matching enum values do not establish full versioned ABI equivalence.

Library strings expose target names `t234`, `t254` and `t256`. These are
investigation leads only, not proof that a named target matches the N1X or
that this package can compile for it. No target is silently selected.
