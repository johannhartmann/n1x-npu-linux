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
separate DLA compiler DLL is listed. The complete archive has now downloaded;
its locally calculated SHA-256 matches the advertised value above. Code
inspection remains incomplete.
Windows binaries are not installed into the Linux Python environment. No
successful compilation, generated DLA binary or physical inference is claimed.

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
A subset of their network-building ABI is exercised below. Profile
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
version emulation above. By default it does not invoke `NvDlaCompilerCompile`.

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
data is evaluated; successful compilation and numerical validation remain
open, and matching enum values do not establish full versioned ABI equivalence.

Library strings expose target names `t234`, `t254` and `t256`. These are
investigation leads only, not proof that a named target matches the N1X or
that this package can compile for it. No target is silently selected.

### Offline compilation attempt

```sh
venv/bin/python scripts/probe_native_graph.py --target t254
```

This optional path opens an ephemeral wisdom directory, assigns the graph
with `NvDlaWisdomSetNetworkTransient(H wisdom, H network)`, creates a named
profile, sets HALF precision and NCxHWx formats, and retrieves the explicit
target. The transient function's second argument is a network handle, not a
boolean; this was verified in its native factory lookups. The sandbox exposes
only its dedicated result directory for persistent output.

All setup calls returned zero, but `NvDlaCompilerCompile` returned **4**.
No loadable was generated. A sandboxed GDB trace confirmed that
`Network::verifyNetworkParams(TargetConfig const*, Profile*)` was reached and
returned **0**; the failure occurs later in compilation. The traced cause is recorded below. A previous call to regular `WisdomSetNetwork` without first
opening wisdom crashed on a null container; the current path opens wisdom
before assignment. These results establish neither target compatibility nor
physical NPU execution.

### Backend 3 is absent from this Linux MTL factory

The native `t254` attempt reaches `Compiler::emit`, then
`MTL::preTranslate` at ELF `0x58d3c0`. GDB captured
`nvdla_mtl_create` arguments `{1,7,0x30000,3}` and later a null first
argument to `nvdla_mtl_pre_translate`. The latter explicitly returns 4 for a
null context. This is a compiler context, not a device handle.

The original factory at `0x648150` allocates a zeroed 24-byte object. For
backend 1 or 2 it calls legacy `mtl_init` and stores its result at +16.
For backend 3 it skips initialization, tests the still-zero +16 pointer,
frees the object and returns null. There is no dynamic loader or callback
on this branch. Changing graph dimensions or adding hardware access cannot
make this factory branch initialize its missing backend.

```sh
venv/bin/python scripts/inspect_mtl_factory.py
```

The verifier executes the pinned factory instructions in Unicorn for backend
values 0 through 4. Allocator/free and legacy `mtl_init` are explicit shims;
the latter returns a synthetic success pointer only to test branch routing.
Backends 1/2 retain that pointer; 0/3/4 return null without calling it.
This proves the missing initialization for this exact ELF, not that legacy
backends compile valid models or that another compiler version behaves alike.
Do not change the backend to 1/2 and label the resulting format N1X-compatible.
The Windows compiler dependency remains the route to investigate for the
missing backend implementation. No machine code has been patched.

### Windows compiler acquired from the SDK

The [official TensorRT download page](https://developer.nvidia.com/tensorrt/download/11x)
links the Windows ARM64 SDK archive
`TensorRT-Enterprise-11.4.0.106-Windows-arm64-cuda-13.4-Release-external.zip`.
Unlike the Python wheel, its central directory contains
`TensorRT-11.4.0.106/bin/nvdla_compiler.dll` (26536048 bytes, compressed
7564073 bytes). The archive is 332016940 bytes. HTTP range requests acquired
its central directory and this entry; the full SDK archive was not downloaded.

```sh
venv/bin/python scripts/fetch_windows_compiler.py
venv/bin/python scripts/inspect_windows_compiler.py
```

The extractor checks response lengths, uncompressed length, ZIP CRC32 and
pinned DLL SHA-256
`0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d`.
This pins the acquired file; it is not an independent Authenticode trust check.
The verifier confirms ARM64 PE, 655 named exports and imports from KERNEL32,
dbghelp and ADVAPI32. It executes the original leaf version getter at RVA
`0x4b2290` in Unicorn: `{1,3,16,13}`, status 0, preserved trailing bytes, and
status 4 for null output. These match the native counterpart and Windows
TensorRT requirement. No DLL constructors, backend factory, Windows APIs or
compiler graph execute in this verifier. The DLL is an ignored analysis
artifact, not installed or committed. Its backend implementation is the next
code-analysis target; successful compilation remains unproved.

### Windows has the missing backend-3 constructor

`scripts/inspect_windows_mtl.py` executes the original Windows factory at RVA
`0x4ca948` for backend values 0–4. This is the counterpart reached from the
Windows MTL constructor at call RVA `0x43ae48`, located through its
`Failed to initialize MTL` string reference. Backends 1/2 call legacy
initialization at `0x4cb148`; backend 3 instead calls `0x626590`.
That function allocates **232 bytes** and invokes constructor `0x617a70`.
The factory retains this object at +16 of its 24-byte wrapper. Thus the
Windows branch differs materially from the Linux factory's null path.

```sh
venv/bin/python scripts/inspect_windows_mtl.py
```

For backend 3, both the factory and object constructor execute original
instructions. Explicit shims implement calloc, operator new and memcpy;
legacy backend initialization is a synthetic success stub only in cases
1/2. No Windows OS calls, graph compilation or hardware submissions execute.
The verifier checks wrapper fields `{0x30000,3,1,7}`, original constructor
completion and vtable RVA `0x114e988`. The constructor initializes its working
path to `./`; this is object data, not a filesystem operation in the verifier.

The vtable includes +16 → `0x624ea0` (stores two configuration pointers),
+32 → `0x624630`, +40 → `0x624eb0`, +64 → `0x624420` and
+80 → `0x629d80`. The Windows wrapper dispatches pre-translation through +32 and translation
through +40, as established below; the native wrapper uses a different slot
for pre-translation. Their complete input contracts still need tracing. Strings and RTTI mention T254/T25x and a
`hummingbird` IR/translator, but these names alone do not establish an
instruction ABI or working compiler. The original Windows object now gives
a concrete implementation to analyze instead of the Linux stub.

### Backend descriptor getters and an internal ABI difference

The Windows MTL pre-translation call at RVA `0x43d2b4` targets wrapper
`0x4cab40`, which dispatches vtable **+32** → `0x624630`. Wrapper
`0x4cac48` dispatches translation through **+40** → `0x624eb0`.
The pinned native ELF pre-translation wrapper instead loads vtable +40.
Equal semantic-version fields therefore do not justify sharing internal C++
objects or blindly transplanting vtables between these builds.

The extended Windows verifier executes these original small methods:

- `0x624ea0` stores its two pointer arguments at object +8 and +16.
- `0x624420` queries buffer size using a one-byte kind argument.
- `0x6243a0` copies a buffer using that same kind argument.

The descriptor vector begins at object +0x58 and ends at +0x60. Its records
are 24 bytes: source pointer at +0, kind byte at +12 and u64 length at +16.
The other record bytes are not assigned meanings. These layouts come from
instruction reads, then synthetic descriptors test them; they are not yet
observed compiler output. Kind names remain unknown.

Size lookup returns zero for an absent kind, skips matching zero-length
records, and returns a special constant 8 for kind 8. It writes exactly one
u64 output. Copy rejects kinds >=8 with raw backend status 2001, returns
`0x7fffffff` for a matching zero-length record, and returns zero without
writing anything when no record matches. Consequently duplicate kinds with
a zero-length first match produce a nonzero size but a failed copy: size
and copy do not select records identically. Tests cover kinds 0, 2, 3, 7,
8 and 9, duplicate/absent records and poisoned output tails.
The copier receives no destination capacity; a future adapter must validate
its own destination allocation and descriptor consistency before invoking it.
These are raw backend statuses, not yet the wrapper's translated NvError.

### Capture real intermediate buffers before the missing native backend

```sh
venv/bin/python scripts/probe_native_graph.py --target t254 --capture-mtl
```

This runs GDB inside the same device-free bubblewrap sandbox. A breakpoint
records the original `MTL::preTranslate` object; the second stops at
`nvdla_mtl_pre_translate` before its null-context check. The pinned library
hash is checked by the Python probe before loading. The debugger copies the
96-byte common argument and bounded, non-null buffers from the object's map
nodes into `results/native-compile-t254/`, then ends the inferior. It does not
change registers, bypass the missing backend, or submit a device operation.
The script permits at most 128 tree nodes and less than 1 MiB per buffer.

For the ReLU graph the captured buffer kinds 0–4 have byte lengths
**44, 4376, 236, 34816, 1368**. Across separate captured processes all five
buffer hashes matched, although process-local addresses changed. The kind-0
buffer contains the initial 44-byte network container. This is an intermediate
compiler object, not the final 256-byte firmware network record. Assigning
firmware meanings to its offsets would be premature.

The map rooted at native object +0x60 supplies pointers; +0xc0 supplies the
first queried size value and +0x90 the other value. For kind 0 those latter
values are 1 and 44, so capture uses +0x90 for the byte extent; other observed
kinds have equal values. The manifest retains the raw maps without claiming
universal count/alignment semantics. `mtl-common.bin` contains live pointers;
they must be relocated and any nested references audited before replay in a
different process or emulator. Raw captured buffers remain ignored artifacts.

### Original Windows collector accepts the captured operation buffers

```sh
venv/bin/python scripts/probe_native_graph.py --target t254 --capture-mtl
venv/bin/python scripts/inspect_windows_mtl_collector.py
```

Windows backend pre-translation `0x624630` first calls `0x756740`, which
invokes input collector `0x756630`. The collector consumes a count at input
+8 and three pointers at +16, +24 and +32. These point to per-operation
regions of **236, 34816 and 4376 bytes**, respectively. Those strides exactly
match the captured native kind-2, kind-3 and kind-1 buffers. Wrapper
`0x4cab40` reconstructs this argument from the common MTL structure; its
network pointer is at +0 of the reconstructed argument.

The verifier checks every captured file against its manifest digest and
length, relocates its pointers into Unicorn memory and executes the original
collector with count 1 and a preallocated output vector. It also executes
the original stack-cookie and stack-probe helper instructions with modeled
TEB/stack memory. Only memcpy is shimmed; vector growth and allocation are
not used by this path.

The resulting record stride is **0x9f60 / 40800 bytes**:

| Record offset | Bytes | Verified source |
| --- | --- | --- |
| 0x0000 | 236 | Native captured kind 2 |
| 0x00ec | 4 | Unwritten poisoned stack padding |
| 0x00f0 | 34816 | Native captured kind 3 |
| 0x88f0 | 4376 | Native captured kind 1 |
| 0x9a08 | 1368 | Unwritten poisoned stack remainder |

All three data regions match byte-for-byte. The whole record copy also
copies the unwritten regions: this verifier deliberately poisons the stack
to expose them and does not assign a semantic meaning to those bytes.
Their presence is not permission to treat them as initialized data in a
future adapter. Neither the captured kind-0 network nor kind-4 buffer is
read by this collector. Its acceptance establishes this copying/layout step
only; it is not validation of every operation field, backend configuration,
IR lowering, an executable loadable or physical firmware compatibility.

### The captured operation reaches the Windows SDP builder

The collector verifier now also executes the original IR dispatch slice at
`0x70e984`, stopping at the selected builder entry before it executes. The
captured surface byte at **+0x1004 is 2**. The dispatch reads that byte,
uses its original jump table and selects builder **`0x6f9928`**. Static
inspection of that builder shows a 0x18f8-byte allocation followed by a call
to constructor `0x6e34e8`; the constructor references the string
**`MtlEngineSdp`** at RVA `0x1166560`. This identifies the selected SDP
builder path, not successful semantic validation of every ReLU parameter.

At the builder entry the verifier checks all seven arguments, in order:
IR context, captured kind-2 common-operation data, kind-1 surface data,
kind-3 operation data, kind-0 network data, kind-4 auxiliary data, and graph
context. Process-local pointers are replaced with emulator addresses; the
captured payload bytes are unchanged. The input's +48 pointer is supplied
from kind 4, matching the dispatcher load. Contexts and the caller's scratch
frame are modeled storage because execution stops before the builder uses
them. No builder, IR object constructor, instruction generator or physical
NPU executes in this dispatch test. The graph's complete Windows translation
remains the next unresolved step.
