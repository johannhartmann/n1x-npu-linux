# A numerical probe for the Windows ARM DLA compiler

> Historical research record. This document preserves observations from earlier
> bring-up stages, including superseded status claims and commands for older builds.
> Use [current status](../status.md) and [setup](../setup.md) for the tested behavior
> and current procedure. Command paths below are relative to the repository root.

The [convolution compiler bridge](convolution-compiler.md) now produces a
separate FP16 1x1 convolution artifact through the same original frontend and
Windows backend. Its constant-pointer relocation and native global-DRAM
materialization are verified against original Windows code. The Linux ioctl
serializer and original firmware scheduler accept it offline; it has not run
on physical hardware.

This includes a reproducible source model, CPU oracle and an offline compiler
investigation. The workflow below now produces a serialized NVDA ReLU fixture
using the original Windows backend and native frontend, joined by checked
result replay. **The resulting fixed FP16 ReLU graph passed on the physical
P16 under Linux on 2026-10-27**, through the manual bootstrap driver; see
[the hardware evidence](p16-board-start.md#physical-numerical-relu-success).
This compiler route does not provide a general Linux model runtime. Earlier
offline results below retain their original limitations. The legacy built-in diagnostic is gated
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
[standalone loadable workflow](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-standalone-mode.html).
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
end-to-end Windows TensorRT compilation or physical inference is claimed.
The hybrid frontend/backend artifact is documented separately below.

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
TEB/stack memory. Only memcpy is shimmed during the collector portion; vector growth and
allocation are not used by that portion. The subsequent constructor/builder
portion and its additional runtime models are described below.

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
them. No builder or constructor executes in this dispatch slice. The same script
now probes construction separately below; no instruction generator or
physical NPU executes. Complete IR construction is checked separately below;
instruction lowering and firmware output remain unresolved.

### Original SDP construction and initial builder execution

The same `inspect_windows_mtl_collector.py` now continues beyond the dispatch
check. It executes original ID-registry constructor `0xb7f40`, the full SDP
constructor `0x6e34e8`, original object-registry constructor `0x6152e8`, and
the required IR/graph container constructors. It then enters SDP builder
`0x6f9928` with the previously verified captured-buffer arguments and pauses
at **`0x6f9a38`** to verify input transfer before continuing below.

The full SDP constructor returns its original object pointer and installs
final vtable **`0x11664d8`**. The early builder allocates a separate SDP
object, registers it through original vector/map code, copies the complete
4376-byte surface into object +0x1b8, and copies the first 156 bytes of the
common-operation buffer into +0x12f0. Both regions are compared byte-for-byte
with the captured inputs; the final vtable is checked again. This establishes
actual object construction and input transfer, not completion of the builder.

Additional runtime modeling is explicit in the JSON output:

- The PE TLS raw template is copied into a modeled single TLS slot, with a
  modeled TEB and stack limit. Original stack protection and CRT static-guard
  code execute against this storage.
- SRW acquire/release/wake calls are single-thread no-ops, and thread ID is 1.
  This does not emulate concurrent Windows behavior.
- Operator new uses bounded poisoned emulator memory. Delete does not reuse
  allocations; all memory disappears when the emulator exits. Memcpy accepts
  bounded copies, including zero-length calls.
- `URID_RANDOM` is explicitly absent, so the ID generator takes its
  deterministic counter path. No random seed or counter return is fabricated.
- Atexit registration returns success; teardown callbacks are not executed.

The required CRT initializers execute before the complete C++ initializer
array, as described below. This still does not reproduce the complete Windows
DLL startup environment. No backend configuration or compiler algorithm is
replaced with a success stub.

### Complete SDP builder and IR graph construction

The verifier now continues the original builder through its return at
`0x6fa024`, with return value **0**. It then uses fresh IR/graph storage to
execute the complete original IR construction routine **`0x70e910`**, including
its dispatch and postprocessing. That routine returns the supplied graph
pointer. Both paths produce the same checked topology:

```text
0_0 (input operand) -> layer-anon-0 (SDP) -> 1_0 (output operand)
```

Checks cover the three pointers in the graph's vector at +0xe0, node/edge
map counts (3/2), successful original edge insertion, and original node lookup
`0x622bf0`. The two operand objects have vtable RVA `0x11c56c0`. Their 248-byte
descriptors at +0x1b8 match captured kind-3 data at offsets 0 and 0x2e8 exactly.
The SDP's four operand ID fields at +0x18e8 are input ID, 0, 0, output ID;
the recorded edges are input-to-SDP and SDP-to-output. Node names are generated
by the original C++ string/stream code and checked, not supplied by a shim.
The SDP surface and common-operation copies remain byte-exact after completion.

The runtime obstacle was incomplete CRT initialization. Original initializer
`0xf190e0` installs the existing static C-locale object `0x18ac930` into global
`0x1a19138`. Without it, locale updater `0xf26f98` reaches abort `0xf11b30`;
this is not evidence of an unsupported NPU operation. Original heap initializer
`0xf267d0` and FLS initializers `0xf1dce0` / `0xf2fa98` also execute. The earlier
description of the dependency as merely diagnostic output was inaccurate:
the stringstream builds operand names needed in the graph.

The additional Windows API model is deliberately limited:

- Import slots are bound by their actual PE import names. Heap allocations
  require the modeled process-heap handle; freed addresses are checked and
  retained without reuse. FLS access requires an allocated slot.
- Library handles are synthetic. Only the enumerated optional CRT capability
  queries are allowed. Optional extended locale APIs and `FlsGetValue2` return
  unavailable (error 127), so original fallback code executes. The recorded
  `AreFileApisANSI` address is supplied; the later path-conversion calls use it.
- Critical sections and SRW operations use a single-thread model; pointer
  encoding is identity. VirtualProtect is modeled against the already RWX PE
  mapping. No host library, filesystem, driver, or Windows API is invoked.
- Atexit/FLS cleanup callbacks are not executed. This is not a complete Windows
  process model. Unicorn's ARM64 MAX CPU model supports the original LDADD
  instruction used in the CRT path.

This establishes **IR construction for the one captured FP16 ReLU graph**.
Backend pretranslation is checked separately below. Instruction translation,
a serialized N1X loadable, firmware acceptance and physical inference remain
separate boundaries.

### Original backend pretranslation returns success

The same verifier now constructs a fresh Windows backend object with original
constructor `0x617a70` and executes its full pretranslation entry **`0x624630`**.
The original code returns **0**, with no nonzero error-flag setter calls.
The graph pointer retained in backend +0xc0 refers to one live allocated
graph, with one node and zero edges. Its node has vtable RVA `0x1150a90` and
name `layer-anon-0`; original graph lookup returns that same node pointer.
These are checked properties of the transformed graph, not a claim that it
already contains executable firmware instructions.

Two additional original DLL initializers are required: `0x19160` builds the
registry at `0x18ce010`, and `0x20fd0` constructs backend data tables including
the table at `0x18d77f0`. Supplying empty replacement tables would not reproduce
this behavior. Both now execute through the original C++ initializer array.

The Windows filesystem fixture contains no `./compile_opt.json`. Its only
attribute query returns false with error 2, so the original default-option
path runs. This is distinct from reproducing the complete Windows frontend's
configuration. The only additional conversion models cover the observed
ASCII path strings, explicit positive lengths, size queries and bounded
UTF-16/ASCII output. They follow the documented
[MultiByteToWideChar](https://learn.microsoft.com/en-us/windows/win32/api/stringapiset/nf-stringapiset-multibytetowidechar)
and [GetFileAttributesExW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileattributesexw)
interfaces for this limited fixture, rather than a general Windows filesystem.

Original code constructs path strings for `addViewGraph`, `rvvHostCopyGraph`,
`rvvFgsConfigGraph`, `rvvPreTranslateGraph` and `rvvRmRvvHostCopyGraph` DOT
outputs. Those strings are recorded; no DOT file is opened or written in this
pretranslation run. The current verifier reserves a bounded 128 MiB emulator
heap for the subsequent translation and retains freed storage without reuse.
Instruction budgets are checked: exhausting one cannot produce a passing result.

### Initial backend translation with the pretranslation descriptor

The verifier now executes original translation entry **`0x624eb0`** through
return value **0**, again with no nonzero error-flag setter calls. The original
code completes its RVV/hardware-layer processing. No external compiler is
launched, and no model-translation function is replaced with a success stub.
This is still a single captured ReLU case using backend constructor defaults,
not a Windows frontend compilation of arbitrary ONNX models.

**Correction from the subsequent frontend trace:** this initial test reused
the pretranslation descriptor when invoking translation. The original
frontend supplies a different second descriptor and assigns input/output
addresses between those calls. The results below remain a reproducible
diagnostic comparison; they are not the final compilation handoff. See the
two-stage workflow below for the corrected translation input and outputs.

The DLL startup code at `0xf03508` calls original initializer runner `0xf19358`
with the range `[0x10523b0, 0x1055af0)`. The verifier executes that same runner
and checks all **1,767 non-null C++ initializer calls in their original order**.
This replaces the earlier selection of individual registry initializers.
Required CRT setup additionally executes low-level I/O initializer `0xf1f750`
and stdio initializer `0xf07dc0`. The modeled process has no inherited handles
or standard console handles. The full Windows DLL startup/loader is not emulated.

The filesystem model supports only the observed fixture: absent
`./compile_opt.json` / `.//compile_opt.json` and virtual directories `/graph`,
`/graph/kernel_compile`, `/graph/linker`. The original read-only OPEN_EXISTING
request for the optional config returns INVALID_HANDLE_VALUE and error 2.
Repeated directory creation returns error 183 and the original code queries
the modeled directory attributes. This follows the narrow observed cases of
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [CreateDirectoryW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createdirectoryw).
None of these requests creates a host directory or opens a host file.
ASCII path conversion also supports the observed null-terminated input case.

`RANDOM_SEED`, `URID_RANDOM`, `RANDOMIZE_UNUSED_REGISTER` and
`VALIDATION_SYNC_STEP` are explicitly absent. Original default branches execute.
The performance counter is synthetic (1 MHz, incrementing 1,000 ticks per
query); it is not usable for performance measurements. All runtime models
and call counts are included in the JSON report.

The backend's vector at +0x58 contains four 24-byte descriptors:

| Kind | Bytes | Observed content | SHA-256 |
| --- | ---: | --- | --- |
| 3 | 256 | Network-descriptor blob in the native consumer contract; fields not yet decoded | `d40c6354059292dc4b2c3c7faca391b59240d305ce45b35da00c902ca67f778c` |
| 0 | 0 | Empty descriptor | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| 6 | 28,736 | RVV memory blob in the native consumer contract; contains layer names | `da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3` |
| 7 | 3,624 | JSON metadata, schema version 1 | `c1af31f16b73278e61f8e6b9b8cf3aedb3140c3caf79afc45c8e4623aa44d76d` |

For each descriptor, original size getter `0x624420` and copy getter `0x6243a0`
are invoked. Nonempty copies match the source bytes exactly and leave a
16-byte destination guard unchanged. The empty descriptor returns size 0 and
copy status `0x7fffffff`, consistent with the independently tested getter ABI;
it is not treated as a successful nonempty copy.

The JSON describes one `gp_layers` entry and four named layers for CPIPE0–3.
Their ES entries have count 2 and offsets 24832, 25216, 25600 and 25984, all
within the kind-6 payload. These values are checked without inferring an ES
record size or writable hardware registers from the offsets alone. Tensor
name lists in this metadata are empty; it does not supply a complete runtime
I/O binding contract.

After all checks pass, the script writes the payloads and provenance manifest
under ignored `results/windows-backend-output/`. No vendor binary or generated
payload is committed. A validation run relocated emulator memory, changed
uninitialized-memory poison from 0xa5 to 0x5a, and changed the counter increment
from 1,000 to 17; **all four output hashes remained identical**. This checks
those sources of accidental dependence for this one fixture, not general
compiler correctness.

These are **backend output buffers, not a complete N1X loadable**. Runtime
packaging, address/relocation bindings, acceptance by the P16 HAL-1 firmware,
physical firmware boot, and numerical inference against the CPU oracle remain
unverified. The next boundary is decoding and packaging these real outputs
through the original compiler/runtime ABI, not submitting the raw buffers to
hardware.

### Native consumers identify the backend blobs

```sh
venv/bin/python scripts/inspect_windows_mtl_collector.py
venv/bin/python scripts/inspect_native_blob_contract.py
```

The second probe executes original native frontend getters and the original
Windows backend copy method against the hash-pinned extracted buffers. It
uses an explicit vtable adapter: native `nvdla_mtl_copy_blob_buf` dispatches
through slot +0x50, while the Windows backend uses +0x48 for its copy method
at RVA `0x6243a0`. Their C++ objects are not directly interchangeable.

| Native consumer | ELF address | Backend kind | Frontend memory kind | Observed result |
| --- | --- | ---: | ---: | --- |
| `getBuiltNetworkDescMemoryBlob` | `0x58ba64` | 3 | 0 | Copies all 256 bytes, status 0 |
| `getBuiltRvvMemoryBlob` | `0x589b40` | 6 | 11 | Copies all 28,736 bytes, status 0 |
| `getBuiltInstructionMemoryBlob` | `0x589aa0` | 0 | No registration on failure | Empty descriptor returns native status 8 |

Both successful copies match the Windows output byte-for-byte. All destination
bytes beyond each payload retain their poison pattern. The empty instruction
copy leaves the entire destination unchanged: the native wrapper maps Windows
status `0x7fffffff` to 8 and skips frontend memory registration. This does not
by itself prove that the complete frontend would fail. The subsequent trace
does show an unconditional instruction getter call at native `0x4cf698` and
an error exit at `0x4cf6a0`. The original Windows frontend similarly calls its
instruction getter at `0x3d0508` and checks the error at `0x3d0510`. The remedy
is the correct translation handoff below, not overriding that failure.

Frontend memory registration and the network-descriptor size lookup are
explicit fixtures in this probe. No frontend compilation is replaced by a
success result. This verifies the selected consumer contract, not complete
packaging, relocation, or validity of the blob contents for firmware. Kind 7
JSON is not classified by this native getter test.

The full Windows translation probe also now invokes original metrics getter
`0x624480` after successful translation. Its source pointer at backend +0x50
is null; it returns `0x7fffffff` and leaves all 36 bytes of the poisoned output
and guard unchanged. No metrics are available in this constructor-default
ReLU fixture. Translation success alone must not be used to manufacture
metadata for a native frontend bridge.

### Correct second handoff produces the instruction and data blobs

Run the stages in order; all vendor files and generated buffers remain local:

```sh
venv/bin/python scripts/probe_native_graph.py --target t254 --capture-mtl
venv/bin/python scripts/inspect_windows_mtl_collector.py
venv/bin/python scripts/probe_native_graph.py --target t254 --capture-translation-input
venv/bin/python scripts/inspect_windows_mtl_collector.py --translation-input
venv/bin/python scripts/inspect_native_blob_contract.py --translation-input
```

The first Windows run now saves its **actual pretranslation mutations** under
`results/windows-pretranslation-output/`. The original routine changes four
bytes of the surface record (offsets 4360–4363) and 18 bytes of the operation
record. The five-buffer sizes and output hashes are checked. It preserves the
64-byte wrapper descriptor, network record, common-operation record and
auxiliary record.

The new GDB capture stays inside the existing device-free native sandbox.
It compares all five live input hashes with the actual Windows pretranslation
run before writing any result. It then replays those output buffers and the
observed return value 0 across the unavailable native backend boundary.
This is a recorded-result adapter for one exact fixture, not execution of a
native backend or an unverified replacement compiler. The native frontend
continues normally and stops before `nvdla_mtl_translate`; it never attempts
loadable execution. GDB unwinds the first wrapper stack instruction when
returning the recorded result.

The native frontend assigns operation-record address-list indices **1 and 2**
at byte offsets **232 and 976**. It constructs a fresh 44-byte network
descriptor with u32 words:

```text
3, 4, 5, 6, 0xffffffff, 7, 0xffffffff, 0, 2, 0, 0
```

Its SHA-256 is `8c52127eee7cbbe584ed4ea87c94632fa30c4623527560074f38f1f4b0a7d9d2`.
The native memory-index map is `{5:3, 6:4, 8:6, 10:5, 11:7}`. Its values are
32-bit at tree-node +36; the original capture helper's generic u64-at-+40
reader was unsuitable for a populated index map and is now corrected.

The second backend descriptor carries that new network pointer and zeroes in
the remaining fields. The original native wrapper takes these from common
offsets 40–88; its earlier common-operation/operation/auxiliary pointers at
offsets 8, 24 and 32 are not those backend fields. The backend retains the
pretranslated graph. The verifier reconstructs the exact second handoff,
allocates a separate network record and preserves the original pretranslation
record. It reruns original pretranslation to retain real backend state, checks
the captured frontend changes, and then executes original translation.

The corrected translation returns 0 and yields:

| Kind | Bytes | Consumer/observed content | SHA-256 |
| --- | ---: | --- | --- |
| 0 | 30,464 | Instruction blob | `eb1090b0e47dc472be8aec0290f0422f61f85ab34d540a807e9ffdc82bee2cbf` |
| 1 | 320 | Data blob | `b42e7caa0ee7f927aa6ce37f2002f1c377493a90772417d690eb0bb9b6e2ebc9` |
| 3 | 256 | Network descriptor | `86f4866d1a6a859b697a8a97548080bcfdc3ef21f06e2f9cb90efc44b75ae781` |
| 4 | 2,738,944 | Preemption context | `1213200f5d0ad268d324b62729dbcd520ff32f4cb94b78bf5afcf536cc1b11a4` |
| 5 | 28,736 | Native stat-kind slot; same bytes as the earlier kind-6 diagnostic output | `da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3` |
| 6 | 0 | Empty RVV-kind descriptor | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| 7 | 10,034 | JSON with tensor/storage metadata | `2b5334a01d3f1d39385f053c8b2baeebe78c7e9e4271b339b98a37b892e9781a` |

Original Windows size/copy methods validate each descriptor and its destination
guard. The native consumer probe separately copies kinds 0, 1, 3 and 4
byte-for-byte through the explicit vtable adapter, returning status 0. The
empty RVV getter returns status 8 when called directly; unlike the instruction
case, native packaging checks RVV size before calling it (`0x4d0cdc–0x4d0d00`).
No dummy instruction or fabricated metadata is needed. The optional metrics
pointer remains null, with the same unchanged-destination error behavior.

JSON metadata now contains one input and output for each of four CPIPE tiles.
Their address-list indices are 1 and 2, FP16 shapes are `[1,32,2,7]` for the
first three tiles and `[1,32,1,7]` for the last. Byte offsets are 0, 448, 896
and 1344; batch/line/surface strides are 3136/224/1568 bytes. These properties
are checked; they do not establish successful device memory access.

Artifacts go to `results/windows-final-backend-output/`. The earlier output
directory remains a distinct diagnostic fixture. A second run changes heap,
stack, TLS and return addresses, poison bytes and synthetic counter increments;
all seven output hashes remain identical. The following stage now serializes
these results. Runtime address binding, firmware acceptance and numerical NPU
execution are still unverified. Backend constructor defaults are still used;
full Windows frontend configuration has not been reproduced.

### Original frontend serializes an NVDA ReLU artifact

After the five commands above, run:

```sh
venv/bin/python scripts/probe_native_graph.py --target t254 --package-windows-output
venv/bin/python scripts/verify_relu_loadable.py
```

`package_windows_output.gdb` repeats the checked pretranslation replay and
compares the next live frontend handoff with the actual Windows final-output
manifest. It supplies those output sizes and bytes at the native MTL wrappers.
The native backend factory remains unavailable; this does not pretend to
implement it. `nvdla_mtl_copy_imatrix_out_data` uses an explicitly modeled
no-output branch: the original native backend-3 branch at
`0x6493cc–0x6493e4` succeeds with a non-null backend and does not modify the
caller output. Its temporary allocation/free side effects are omitted.

Everything after those backend boundaries, including `setLoadableContents`,
the serializer and the public size/image getters, runs as original native
compiler code. The recorded wrapper sequence is one size query, one imatrix
query and copies of kinds **3, 0, 1, 5, 4**. No kind-6 copy occurs because its
size is zero. The native process exits normally with all three public
compile/size/image calls returning 0.

The generated local file is `results/native-compile-t254/relu.nvda`:

- Size: **2,800,596 bytes**.
- SHA-256: `561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942`.
- FlatBuffers identifier/version: **NVDA / 0.9.0**.
- One task, one submit, nine memory records, nine address records and two
  tensor records in the bounded shared-field projection.
- Five copied blobs named `task-0-nw`, `task-0-inst`, `task-0-ctrl_data`,
  `task-0-preempt_ctx` and `task-0-stats`, plus three pool-size properties.
  The five blobs use interface 1, version **3.0.0** and match the original
  Windows output bytes exactly.

The task address list is `[3,1,2,4,5,6,7,8]`. Input/output memory IDs 1 and 2
each have 3136 bytes and input/output flags respectively. Memory IDs 3–7
contain the five named blobs at offset zero; all memory alignments are 4096.
The local instruction/data/preemption/stat indices from the frontend map
refer to positions in that task list, rather than directly to global memory
IDs. This distinction matters when building the Linux submission path.

The 256-byte compiler network descriptor contains nonzero extension values:
`+0xcc=0x3054`, `+0xd0=4`, `+0xd4=0x3054`, `+0xd8=3`. These are actual compiler
output, not guessed patches to the older diagnostic's zero padding. Their
original firmware/IRQ consumers are exercised below in RAM; physical engine
behavior still needs qualification.

The verifier checks the pinned artifact hash, task/submit relationships,
shared-field projection, memory bounds for the named content and exact blob
bytes. Two packaging executions produced the same file hash. It deliberately
does not claim that a partial NVDLA-v1 projection validates every field of the
modern 0.9.0 schema. JSON kind 7 remains a separate research artifact.

This is now a **serialized compiler artifact for the one exact ReLU fixture**.
It is not a general Windows compiler port or proof of physical firmware
compatibility. The next stages below exercise the original runtime validation,
content materialization and firmware scheduler separately. Complete runtime
initialization and eventual numerical execution on the NPU remain outstanding.

### Original runtime validation and content materialization

```sh
make test-relu
```

This target requires the generated source/oracle and serialized artifact from
the preceding workflows. It runs the artifact verifier and the eight offline
runtime/firmware probes. It neither loads a module nor accesses hardware.

`inspect_loadable_version_gate.py --relu` enters the original Windows
deserializer at **0x888a0**. All five original validators at **0x80518,
0x80f40, 0x80a00, 0x80130 and 0x7ff08** execute against the complete generated
file. The root-major check then reaches **0x889cc**, the first materialization
boundary. Only mutex operations are modeled; none of the validators is
replaced by a successful result. Negative cases reject a changed major
version with status **0x430003**, and a short root word, an out-of-range root
pointer and a truncated payload with status **11**. These negative changes
exist only in emulator RAM.

The content-version check now uses the original provider chain rather than
an invented response: runtime vtable +0x1d0 → **0x25410** → vtable +0x200 →
**0x25460**, with device-count getter **0x25630**. On an empty cache it calls
**0x75810**, which returns the constant **0x30000** and does **not** query a
device. The original converter emits/caches `{3,0,0}`; a second case exercises
the already cached path. The actual network blob's required version `{3,0,0}`
passes comparison with status 0, so this fixture does not need selector 3's
error-bypass behavior. Selected-device/cache storage and CFG dispatch remain
explicit fixtures. Matching the software constant is not a firmware handshake.

`inspect_content_materialization.py --relu` runs original content materializer
**0x88160**, getter **0x8b350**, and the bounded normal-loader copy slice on all
**eight** embedded contents. Payloads remain byte-identical; the 256-byte
network descriptor is not expanded or patched. The five task blobs retain
interface 1/version 3.0.0. `task-0-stats` matches the recovered ASCII predicate
`^task-?(\d+)-stat(s|_list)$` and the original branch assigns sub-interface 8.
The regex helper and C++ containers are modeled as in the older diagnostic
probe. Every undersized copy destination is rejected without a copy; exact
sizes copy correctly and preserve a 32-byte guard.

Those two probes cover the validator prefix and content stages. The separate
probe below extends coverage through the outer deserializer's return; device
initialization and later normal-loader checks are still not covered.

### Complete outer deserialization and its statistics limitation

`inspect_full_deserialization.py` runs the original loadable constructor
**0x7e2d8**, checks its vtable **0x669260** and deserializer slot **0x669328**,
then calls **0x888a0** with the complete pinned ReLU artifact. The original
constructor, all five prefix validators, reference validator **0x8deb0**, all
eight table materializers, and final statistics-index function **0x8c740**
execute. The outer function returns **0** and restores the caller's stack.
The input file is unchanged.

Unlike the isolated content probe, this test executes the original C++ map,
vector and string implementations. Allocation/free, byte copy/fill/compare,
single-thread mutex operations, CFG dispatch, atexit registration and the
required Windows APIs remain modeled. The normal cases execute the original
regex compiler and matcher, including predicate **0x7d970**. Both zero-filled and 0xa5-filled heap
allocations/initial stacks produce the same checked fields. The original
stack guards **0x1000/0x1020** must execute: they reserve/release 16 bytes, and
replacing them with no-ops can corrupt nested frames. The older content probe
now also executes those guards.

The original in-memory records agree with an independent FlatBuffers field
projection at these locations. These are private C++ object layouts in the
pinned DLL, **not** firmware or kernel ABI structures:

| Root vector | Object vector offset | Record bytes | Checked records |
| --- | --- | --- | --- |
| Memory, field 2 | `+0xe0` | 80 | 9, including sizes, alignment, flags and content offsets |
| Tasks, field 1 | `+0xf8` | 88 | 1, including interface/instance and all three ID lists |
| Submits, field 8 | `+0x110` | 32 | 1, task ID 0 |
| Events, field 4 | `+0x128` | 12 | 1, exact materialized bytes |
| Addresses, field 3 | `+0x140` | 24 | 9, memory ID, size and offset |
| Tensors, field 6 | `+0x158` | 112 | 2, names, IDs, extents, formats, dimensions and stride fields |
| Relocations, field 7 | `+0x170` | 32 | 0; nonempty relocation materialization is not qualified |

The content map at **+0xd0** contains eight original tree nodes. Their keys
are ordered and all payloads, sizes, interface/sub-interface fields and content
versions agree byte-for-byte. The task's local address order remains
**`[3,1,2,4,5,6,7,8]`**, as used by the Linux C task/firmware scheduler test.
The original runtime does not rearrange this list during deserialization.

The probe first executes these original initialization functions; each CRT
initializer returns 1. This is the subset required by the parser, not complete
DLL attachment, its whole initializer array, or device initialization:

| RVA | Observed initialization |
| --- | --- |
| `0x5f5a0` | Encoded optional-function cache using the image's security cookie |
| `0x5f650` | Process heap handle |
| `0x56aa0` | CRT initial-data pointer `0x6a45f8 → 0x6a1170` |
| `0x585f0` | CRT thread data and its FLS slot |
| `0x4df38` | C++ runtime FLS slot and initial thread record |
| `0x3490` | Static regex, including original constructor `0x7dd70` and compiler `0x83840` |

Windows API calls use explicit single-thread heap/FLS/lock models. Dynamic
optional API lookups report unavailable modules; the original C-locale
fallback executes using the imported base API models. No host Windows APIs,
files, processes or devices are accessed. The compiled regex at **0x6a5b10**
is now real original-code output, and both content classification and the
statistics function use its original matcher/capture processing.

**The outer return value is not proof that every stage succeeded.** After
these initializers the final statistics-index function returns **2**, but its
caller ignores that value at **0x88b0c** and returns 0. This is now a concrete
header mismatch between TensorRT 11.4 compiler output and runtime 617.42:

| Statistics bytes | Compiler output | Original runtime check at `0x8cf44–0x8cf64` |
| --- | --- | --- |
| `+0..+2` | `00 03 00` | Checks `+0=0`, `+1=3`; does not compare `+2` here |
| `+3..+5` | `00 02 00` | Checks `+3=0`, **`+4=1`**; does not compare `+5` here |
| `+8..+11` | `04 00 00 00` | Old format reads this as record count after accepting the version |

The rejection follows **0x8cf64 → 0x8d164**, selecting status 2. The
statistics map at **+0x1b0** stays empty and its dirty flag at **+0x1c0**
becomes 0. The shared content version 3.0.0 therefore does not establish
statistics-format compatibility. This is an old-runtime/new-compiler mismatch;
it does not prove that the newer compiler's statistics are internally invalid.

A negative control still omits the initializers and retains the modeled
ASCII predicate used by the older probe. It reproduces status **8** through
**0x8c998 → 0x8ca68 → 0x8d238** because no compiled regex/capture is available.
That earlier error is separate from the now-visible statistics header check.

An explicitly **unsafe version-only mutation**, performed only in emulator
RAM, changes statistics byte `+4` from 2 to 1. The original statistics function
then returns 0 and creates one index entry, but its recorded extent is only
**3,392 bytes**, calculated as `(4*0x340 + 0x4b) & ~0x3f`. The actual compiler
blob is **28,736 bytes**. Its four layer names are at offsets `0x1c`, `0x181c`,
`0x301c`, `0x481c`, spaced by `0x1800`, rather than the old parser's `0x340`
record stride. A version-byte rewrite is consequently **not** a conversion or
a valid compatibility fix. The checked artifact remains unchanged. The
[original producer and Linux layout parser](statistics-v2.md) now establish
the newer header, strides, and per-record data offsets. Internal fields,
counter decoding and hardware statistics collection remain unverified.

Three in-memory mutations test the extent of the original checks:

| Mutation | Original outer status | Observation |
| --- | --- | --- |
| Address 3 references nonexistent memory ID 99 | `0x30007` | Reference validator rejects before materialization |
| Task's first address ID is 99 | `0` | Bad ID is preserved in the materialized task |
| Memory 3 names `Xask-0-nw`, absent from content map | `0` | Missing name is preserved in the materialized memory record |

The latter two are **not successful loads or valid execution inputs**. They
show that the deserializer alone does not check all relationships; later
loader checks may still reject them. A Linux model loader must verify task
IDs, address/memory references, named content and extents before submission.
The current RAM fixture helper accepts only pinned artifacts, and the
mutated files are never passed to its firmware task runner or any device.
No general model loader, full runtime initialization or physical compatibility
is established by this probe.

### Complete original memory-content initialization

`inspect_memory_initialization.py` runs the original memory loader at runtime
RVA **0x20d00** through its return at **0x210b8**, after the full constructor,
CRT/regex setup and deserialization described above. It reuses the original
materialized C++ name/offset vectors and content map. Getter **0x8b350**, string
operations, version-provider chain, copy loop, cleanup and callback **0x1b280**
execute original instructions. Allocation, byte-copy/fill, mutex and CFG
boundaries retain the existing emulator models.

The allocator-owned memory descriptor is an explicit RAM fixture, with the
following fields observed in this function (not a public or device wire ABI):

| Offset | Observed use |
| --- | --- |
| `0x00` | Owner passed to the post-copy callback |
| `0x18` | Destination extent |
| `0x25` | Initialization flag byte, tested with mask `2` |
| `0x30` | Vector of 32-byte C++ content-name strings |
| `0x48` | Vector of 64-bit destination offsets |
| `0x60` | CPU destination pointer |

The runtime/provider context is also synthetic. Original provider code fills
its version cache with `3.0.0`; this is a software constant, not a device
handshake. All five ReLU initializers pass the original interface-1 and
dependency-version checks. They copy **2,798,720 bytes** in total: network 256,
instructions 30,464, control data 320, preemption context 2,738,944 and
statistics 28,736. Every byte matches the serialized model, and poisoned guard
bytes remain unchanged. The other four memory records have no initialization
flag and remain untouched, including input and output. No data transformation
or missing network suffix is synthesized in this function.

Fourteen additional RAM cases establish these boundaries:

| Input | Original status / effect |
| --- | --- |
| Null model or memory descriptor | `4`, no copy |
| Different name/offset vector lengths | `4`, no copy |
| Name absent from the content map | `11`, no copy for that name |
| Content plus offset exceeds destination extent | `6`, no copy for that content |
| Nonzero offset ending exactly at the destination boundary | `0`, exact copy |
| Initialization flag clear | `0`, no lookup/copy, even with mismatched vectors |
| Initialized memory with empty vectors | `0`, no copy |
| Two valid contents | `0`, copied in vector order |
| Second name missing or second content out of bounds | `11` or `6`; the first copy remains |
| Overlapping content destinations | `0`; later content overwrites earlier bytes |

This resolves the missing-name question left by deserialization: the content
loader rejects it when that memory entry is initialized. It also establishes
that this loader is **not transactional**. A Linux loader should resolve and
validate all selected contents and destination extents before the first copy,
using subtraction-based bounds checks rather than unchecked offset-plus-size
arithmetic. If matching the observed overlapping-content behavior, copies
must preserve vector order. CPU allocations must remain private until the
complete preparation succeeds; this test supplies no DMA ownership guarantees.

The original statistics-index error still occurs earlier (status `2`, ignored
by the outer deserializer), while this function copies the newer statistics
blob successfully. Copy success therefore does not establish compatibility
with the old statistics consumer. No original allocator, complete model load,
device submission, physical firmware execution or numerical inference is
claimed. Vendor files and the serialized input remain unchanged.

### Native named-content initialization

`n1x_dla_initialize_contents()` in `driver/n1x_dla_model.c` prepares private CPU
buffers from the recovered named-content/offset contract. It accepts allocator
views of the memory table, bounded catalog entries, and a flattened list of
selected initialization references. The caller must check dense memory IDs,
name/offset vector lengths and select only memory entries whose initialization
flag is set. Names remain length-delimited bytes throughout the C lookup.

Before any write, the helper checks all catalog name views and every selected
reference: the memory index must exist, the name must match exactly one catalog
entry, a nonempty payload must have a source pointer, logical memory must fit
its backing allocation, and the copy must fit the logical extent. The extent
test uses `bytes <= logical_bytes - offset` after checking the offset, avoiding
overflow and preventing allocator padding from enlarging model content. A
missing or ambiguous selected name, invalid index, pointer or extent returns
`-EINVAL` without changing any destination, including earlier memories.

Only after the entire list passes does it copy contents in input order.
Overlapping destination ranges retain the Windows loader's ordered-overwrite
behavior. Zero-byte contents are permitted, including at the logical end.
Uninitialized regions and padding retain their previous contents. The caller
must keep all metadata, names and source bytes immutable and separate from
destinations during the call, and supply actual accessible allocations. The
helper cannot validate arbitrary pointers or provide atomicity against a CPU
fault/concurrent writer. It allocates nothing and performs no device access.

`test_model_contents.py` checks bounded name matching (including embedded NUL
bytes), missing/duplicate selected names, multiple memories, repeated contents,
ordered overlaps, exact-end copies, zero-byte contents, logical overrun despite
available padding, large offsets/sizes, invalid indices and null inputs. Late
errors must preserve every poisoned destination, with prefix/suffix guards,
padding and all source views checked unchanged. The ReLU projection supplies
all eight catalog blobs and five initializers to the native helper; its whole
RAM image matches the independent Python fixture byte for byte.

`test_relu_scheduler.py` now passes the **C-initialized** image to the original
HAL-1 firmware in all eight IRQ/status cases. Python still projects the pinned
FlatBuffers model and supplies trusted allocation extents, but no longer
performs the content copying for the submitted emulated image. The existing
Python-generated image is used only as a comparison. Input samples, poisoned
output and task/fence headers are installed afterward as before.

`make kernel-check` also builds this helper against the installed kernel headers.
It is not linked into the observation or experimental bootstrap modules, and
is not a general model loader: content versions, instruction semantics, device
allocation, DMA synchronization, buffer lifetime and hardware submission remain
separate work. Successful memory preparation does not resolve the statistics
consumer mismatch or prove physical NPU compatibility.

### Native model-address resolution

`n1x_dla_resolve_task_buffers()` in `driver/n1x_dla_task.c` resolves the
model's global address IDs into the task's local order. Its caller supplies
dense, already ID-checked memory/address tables and allocator-owned mapping
extents. The helper checks every selected reference before writing any output:
task indices must be strictly below the address count, memory indices must
exist, logical memory must fit the mapping, address subranges must fit logical
memory, and address arithmetic must not wrap. Empty extents and lists outside
the qualified cached-path range of 1..512 entries are rejected.

Each result keeps three values separate:

- Device address: allocation address plus the model address offset.
- Logical bytes: the declared model address subrange.
- Mapped bytes: the remaining accessible allocation extent, including any
  allocator-provided padding or following content in that same allocation.

Mapped bytes do not enlarge logical model content. Cache-read validation can
use the mapping extent, while instruction/content bounds still need the
logical extent. This helper does not validate embedded instruction indices,
register accesses, hardware address width, buffer-specific alignment, all
unselected model entries or actual DMA ownership/lifetime. Inputs must remain
immutable for the call and must not overlap output. It performs no allocation
or device operations and preserves output on rejection.

`test_model_addresses.py` checks padded subranges, reordered/repeated
references, 512 entries, invalid/equal-to-count indices, zero extents, logical
overrun despite sufficient backing, arithmetic overflow, short output and
null arguments. The pinned ReLU model's results are compared with its
independent RAM layout. `test_relu_scheduler.py` then uses the C resolver's
addresses and `n1x_dla_build_address_list()` output directly in all eight
original-firmware scheduler/IRQ cases. The wire list matches the independently
expected eight little-endian addresses and 192 padding bytes.

This connects native address validation to the existing firmware test. It is
not a general FlatBuffers loader or a physical submission path: the Python
caller still supplies the pinned model projection and trusted RAM allocations.

### Unmodified HAL-1 firmware processes the real ReLU task

`test_relu_scheduler.py` uses the generated artifact, the native content
initializer from `driver/n1x_dla_model.c`, and the Linux C fenced-task serializer
and checked address resolver from `driver/n1x_dla_task.c`. It builds eight task-local
addresses in order `[3,1,2,4,5,6,7,8]`, preserving the distinction between
task-local positions and global address IDs. The input contains the checked
3136-byte CHW16 FP16 source; output starts as 3136 bytes of 0xa5. No expected
numerical result is injected into output storage.

The original HAL-1 firmware runs from reset, accepts the task through its
original trap/command path, DMA-fetches all 256 initialized network bytes,
fetches the real control record and instruction data, and processes the
instruction-generated address/register writes. Seven observed address pairs
at modeled offsets `0x2505c + 8*i` match the first seven task-local addresses.
These are observations of this fixture, not an independently qualified host
register-programming interface. The scheduler reaches its engine wait at
return address **0x101424**, with one remaining control record.

The original engine IRQ handler **0x1025e6** reads the compiler's two
offset/mask pairs: `{0x3054,4}` at network +0xcc/+0xd0 and `{0x3054,3}` at
+0xd4/+0xd8. The test injects firmware-local IRQ bit **0x400** or **0x800**
through the original trap and supplies a RAM value for engine offset 0x3054:

| Modeled status | Observed result in running state 1 |
| ---: | --- |
| 0, 2, 3, 4, 6 or 7 | Remains waiting, record count 1, original fence unchanged, no task IRQ |
| 1 or 5 | Record count 0, engine/queue idle, original postaction writes fence `0x12345678`, task IRQ bit 0x40 asserted |
| `0xfffffff8` through `0xfffffffb` | Only `0xfffffff9` completes; the other three remain pending |

The expanded matrix has 48 cases: twelve status words, two IRQ sources and
two DMA timings. All return through original trap code. In running state 1,
the original ISR at `0x1026f0..0x1026fe` requires **`(status & 3) == 1`**;
testing bit 0 alone would incorrectly accept 3 and 7. This is not a general
decode of every status bit or of other firmware execution states.
Completion uses the
original scheduler, postaction and DMA code; no engine counter, firmware code,
fence or queue state is patched to manufacture that result. The injected IRQ
and status are explicit simulated hardware inputs. The actual firmware code
bytes are checked unchanged, as are input and poisoned output buffers.

This resolves the earlier **zero-extension barrier for this newly compiled
fixture**: its suffix comes from the real compiler and supports the observed
completion path. It does not repair the legacy diagnostic or prove equivalence
to the opaque SROOT executable. There is no arithmetic-engine simulation,
physical GIC delivery, measured DMA coherence or numerical NPU inference here.
The successful cases prove control flow under modeled completion, not computed
ReLU output. Hardware execution remains a separate qualification step.


### ReLU firmware retirement boundary (2026-10-27)

`test_relu_scheduler.py` now stops immediately after the original firmware's
SWGEN0 completion store, before returning to scheduler WFI. Across both modeled
engine IRQ sources (0x400 and 0x800) and completion-status variants 1 and 5, the
observed boundary is PC `0x10222a` after the store at `0x102228`:

| State at SWGEN0 | Observed value |
| --- | --- |
| Task DMA commands | 24 |
| Queue count | 0 |
| Firmware task-slot in-use | 0 |
| Remaining engine records | 0 |
| Active-engine state | 0 |
| Postaction fence | `0x12345678` |

The test unmaps **all** device-visible model/task backing at that point and
resumes the original firmware tail. It reaches scheduler WFI without another
DMA command or access to the revoked memory. The noncompletion status variants
remain incomplete: no SWGEN0, no retirement boundary and no release.

The emulator still supplies engine status/IRQ and synthetic DMA completion;
it does not simulate engine arithmetic or an engine performing late physical
DMA independently of firmware. Output remains poisoned in every case.
This strengthens the firmware-side lifetime evidence beyond the bypass case,
but is not another numerical hardware result or permission to free buffers on
errors. Physical numerical execution is recorded in `p16-board-start.md`.
Local records: `results/relu-retirement-boundary.json` and
`results/relu-retirement-ubsan.json`.

The same matrix now runs with both instantaneous DMA and a 17-status-read
delay before each modeled copy (48 cases after the full status matrix expansion).
Completion records prove
that all 24 original firmware DMA commands have completed at SWGEN0, including
the final postaction-fence write. Each transfer must incur the requested
delay. Revoking the backing after that signal still permits the firmware tail
to return without further DMA. Noncompletion engine statuses remain pending
under both DMA timings. This exercises original firmware waiting behavior,
not real device memory ordering. Result: `results/relu-delayed-dma.json`.
The expanded status matrix is recorded in
`results/relu-retirement-status-matrix.json`.
