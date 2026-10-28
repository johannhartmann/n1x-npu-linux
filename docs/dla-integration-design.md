# How to expose N1X DLA to Linux applications

Research date: 2026-10-28. This records the architectural recommendation; the experimental native
implementation is tracked in [native-runtime.md](native-runtime.md). It does
not claim NVIDIA library compatibility. No hardware state
was changed during this investigation.

## Decision

Build a native standalone runtime over a Linux DRM accel driver. Preserve the
cuDLA programming concepts: device, loadable module, tensor descriptions,
registered buffers, submission, completion and explicit asynchronous errors.
Use a small C API and CLI first, followed by a thin Python binding. Treat
actual cuDLA compatibility and CUDA interoperability as separate, testable
integration layers. Do not claim that creating `/dev/accel/accel0` makes NVIDIA
TensorRT or its binary `libcudla` use our driver automatically.

## Sources inspected

The upstream [NVIDIA Deep-Learning-Accelerator-SW repository](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/tree/25a39b076c6b9143a25adbce8cb468b3bee821ee)
was cloned locally and inspected at commit
`25a39b076c6b9143a25adbce8cb468b3bee821ee` (Apache-2.0 relicensing).
The relevant files are:

- [Standalone sample](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsStandalone/main.cpp):
  device/module creation at lines 526–536, tensor metadata at 554–655,
  external buffer import at 969–1016, synchronization at 1077–1197,
  unregister at 1274–1303.
- [Hybrid sample](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsHybrid/main.cu):
  CUDA allocations at 526–563, registration at 599–632, task/stream execution
  at 680–745, statistics translation at 758–808, teardown at 822–875.
- The two sample Makefiles, sample READMEs, `FAQ/README.md`,
  `scripts/prepare_models/`, `tools/qdq-translator/` and `operators/`.
- Current [cuDLA documentation](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/index.html#cudla),
  [TensorRT platform support](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/work-with-dla.html),
  [Windows ARM restrictions](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-windows-on-arm.html),
  [standalone loadables](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-standalone-mode.html)
  and [Linux accel](https://docs.kernel.org/accel/introduction.html).
- Our local R38 `nvhost_nvdla_ioctl.h` and NVDLA host1x device code, the native
  bootstrap, Windows transport analysis and the physical ReLU result.

## What the NVIDIA repository actually supplies

It contains application examples, model preparation, quantization translation,
operator reconstruction and documentation. It does not contain the kernel
implementation, the cuDLA/runtime implementation or the DLA compiler source.
Its examples link external libraries and cannot replace the missing platform
backend. The standalone executable links `cudla`, `nvscibuf` and `nvscisync`;
the hybrid executable links `cudla` using the CUDA toolchain.

Both sample Makefiles explicitly disable the sample for `TARGET_ARCH=sbsa`
(lines 278–282). They can therefore report a waived build rather than produce
a runnable application. Removing that guard or adding SM121 to the CUDA
architecture list would not implement the missing DLA backend. This is a
sample platform restriction, not evidence that the N1X hardware cannot run DLA.

| Concern | Standalone sample | Hybrid sample |
| --- | --- | --- |
| Device | `CUDLA_STANDALONE` | `CUDLA_CUDA_DLA` with CUDA context |
| Model | `cudlaModuleLoadFromMemory`, tensor attribute queries | Same model lifecycle |
| Memory | NvSciBuf allocation, `cudlaImportExternalMemory` | `cudaMalloc`, `cudlaMemRegister` |
| Submit | `cudlaSubmitTask(..., NULL, ...)` | `cudlaSubmitTask(..., stream, ...)` |
| Completion | Imported NvSciSync semaphore and EOF fence | CUDA stream synchronization |
| Statistics | Extra registered outputs and translation export table | Same mechanism |

The standalone sample README says that CUDA runs its inference, but its actual
source uses standalone device creation and NvSci synchronization. The code and
cuDLA guide establish the distinction. The sample's SM architecture list is
not evidence that any GPU with that SM version has a usable DLA.

The profiling examples are not numerical acceptance tests: they copy outputs
but do not compare against an independent reference. The standalone sample
also waits indefinitely and does not call `cudlaGetLastError`. The cuDLA guide
requires that extra error query after standalone synchronization. Our API must
provide bounded waits and job error status rather than copying those omissions.

## Limits of transferring Orin recipes to this P16

The repository largely describes Jetson/DRIVE Orin-era deployment. Its INT8
and QDQ-removal recipes must not become our default model pipeline. NVIDIA's
current Windows ARM specification has fixed shapes, batch 1, FP16 compute by
default, no pure INT8 compute and no automatic GPU fallback. Quantized boundary
formats are not evidence of INT8 arithmetic. These are software support limits,
not a proof about every operation possible in N1X silicon.

The Windows ARM support table describes exact validated operation/shape/type
tuples, not every combination of the listed attributes. Use those tuples to
choose the first convolution and multi-layer fixtures. That page also requires
rebuilding the 11.4 beta engines for general availability and calls out an
intermittent accuracy issue. Record compiler versions with the artifacts and
check repeated numerical results; successful compilation and one correct
execution are insufficient acceptance criteria.

TensorRT 11.4 explicitly excludes Linux SBSA DLA support. Its generic advice to
retain 10.7 on older platforms does not demonstrate N1X compatibility. The
existence of the native compiler package also does not establish a working
N1X backend: our successful artifact still used the checked native frontend /
original Windows backend route described in [model-probe.md](model-probe.md).
An arbitrary ONNX-to-N1X compilation pipeline remains unfinished.

The repo examples use older `--int8`, `--fp16` and `--buildDLAStandalone`
commands. Current TensorRT documentation instead describes strongly typed
networks and `--useDLACore` plus `--safe` for standalone export. Command recipes
must be pinned to the compiler version; neither recipe is an installed, working
N1X Linux compiler merely because the documentation shows it.

A TensorRT engine and a standalone DLA loadable are distinct artifacts. Model
compilation, loadable interpretation and kernel job submission must remain
separate. Keep source ONNX, compiler/firmware target versions, tensor layouts,
strides, padding and reference data alongside each loadable. Do not treat Orin
loadables as portable to N1X because both runtimes use DLA terminology.

Operator reconstruction is useful as a graph transformation/reference suite.
The repository's `operators/common/onnxruntime_utils.py` executes ONNX Runtime;
passing those tests does not establish N1X compiler acceptance or NPU execution.
For this board, retain FP16 first and qualify each larger graph numerically.

## Recommended division of responsibility

```text
ONNX / explicit graph
  -> target-specific offline compiler
  -> versioned N1X DLA loadable + tensor metadata
  -> native userspace runtime (C API, CLI, then Python)
  -> driver-specific ioctls on /dev/accel/accelN
  -> N1X firmware and DLA
```

The kernel owns power/firmware startup, IOMMU mappings, buffer lifetime,
validated job descriptors, completion/error handling and recovery. It exposes
buffer handles and job identifiers rather than physical addresses or writable
MMIO. Use the accel/DRM infrastructure for the device and suitable standard
buffer/fence mechanisms; it does not supply a universal inference ABI.

Userspace owns loadable parsing, version/target checks, tensor metadata,
materialization, layout conversion and model-level error messages. The kernel
must independently validate handles, ranges and supported command structures;
a trusted userspace parser is not a substitute for isolation. Arbitrary
firmware instruction streams must not be exposed to untrusted users before
their address/register access and isolation properties have been qualified.

Jetson's inspected R38 path is a vendor `nvhost` interface with NVDLA ioctls,
host1x-related resources and fences. It is not the generic Linux accel API.
The Windows stack instead uses D3DKMT transport. Those are backend references,
not reasons to recreate Windows or all of host1x in our driver. Reuse the
verified firmware contract and native Linux mechanisms.

Initially serialize submissions on one queue. Provide discovery/capabilities,
buffer allocation/mapping, model/module metadata, submit, bounded wait/status
and close/release semantics. This is a proposed API shape, not a frozen list of
ioctl numbers. Export/import through DMA-BUF and synchronization through
DMA fences/sync files can follow when actual mappings and ownership are tested.
Shared DRAM alone does not make CUDA pointers valid DLA addresses.

## cuDLA, TensorRT and framework integration

### Integration choices for this machine

The source checkout was rechecked against upstream HEAD on 2026-10-28; both
still resolve to `25a39b076c6b9143a25adbce8cb468b3bee821ee`.

| Approach | What would have to work | Recommendation |
| --- | --- | --- |
| Run the upstream samples unchanged | Compatible NVIDIA `libcudla`, NvSci for standalone, platform kernel ABI and target loadables | Reference applications, not an installation path demonstrated on N1X Linux |
| Port the Jetson `nvhost` stack wholesale | Its host1x, memory, synchronization and platform dependencies | Use it to understand contracts; do not make recreating that platform a prerequisite |
| Native N1X accel driver and runtime | Our verified firmware transport, model loader, completion and memory lifetime | First usable integration target |
| Replace `libcudla.so` | Full advertised API semantics, opaque object interoperability and application compatibility | Defer; a few matching exported function names are insufficient |
| Framework execution provider | Working native runtime, target compiler, supported-subgraph selection and numerical tests | Add after standalone execution is dependable |

The proposed native lifecycle is `open -> load -> allocate/bind -> submit ->
wait/status -> read -> release`. This describes the target, not the current C
ABI: `runtime/n1x_dla.h` presently offers open/info/submit/read, userspace
loadable parsing lives separately, and EXEC waits synchronously. A separate
asynchronous queue API is unnecessary for the first numerical qualification.
Similarly, begin with explicit host copies; DMA-BUF and sync-file support only
become useful after both the DLA and GPU import/export paths are verified.

The immediate release gate is physical repetition with distinct inputs and
correct results, followed by proven safe buffer reuse/reclamation and a
bounded failure/recovery path. The current 64-MiB/128-job retention limits
make the experimental API unsuitable for a long-running inference service.
The [Windows unregister trace](windows-buffer-lifetime.md) also shows why
copying vendor API teardown is insufficient: its destruction flags permit
Windows to defer reclamation until pending GPU work completes. Linux needs
its own demonstrated device-lifetime guarantee.

The first native runtime should follow cuDLA's lifecycle without pretending to
be NVIDIA's binary library. A true standalone compatibility adapter must handle
NvSci objects and asynchronous errors faithfully; opaque NvSci handles cannot
be replaced by unrelated native pointers. Hybrid compatibility additionally
requires correct CUDA ordering, memory sharing and error propagation. The
current cuDLA guide lists restrictions including UVM, VMM and CUDA Graphs;
we should advertise only tested capabilities for the specific API version.

Do not install an incomplete replacement as system `libcudla.so` or assume
TensorRT discovers the NPU from a device-node name. Reusing an official ELF
runtime remains an option only after validating its N1X target support,
dependencies and kernel ABI. The current local CUDA tree and linker cache do
not provide cuDLA/NvSci libraries; the acquired runtime counterparts are Windows
DLLs. This inventory is not a claim that no suitable package can ever exist.

After the native runtime works, first test explicit GPU preprocessing -> DLA
subgraph -> GPU/CPU postprocessing with real buffer/fence ownership. Add a
framework backend, such as an ONNX Runtime execution provider, only when model
compilation and supported-subgraph execution are dependable. Such partitioning
would be our integration work, not existing automatic N1X GPU fallback.

## Implementation order and acceptance criteria

### Source-to-implementation review

The upstream checkout and remote HEAD were checked again on 2026-10-28 at
`25a39b076c6b9143a25adbce8cb468b3bee821ee`. Reading the actual sample call paths,
rather than just their READMEs, yields the following implementation checklist:

| Upstream behavior | N1X implementation consequence | Current gap |
| --- | --- | --- |
| Both samples load a precompiled module and query tensor descriptions before allocating buffers | Keep compilation separate from runtime; validate target/version and honor byte sizes, strides and padding | Native NVDA parsing covers a checked single-task DRAM subset; general model compilation and other loadable features remain open |
| Standalone imports NvSciBuf objects and an NvSciSync semaphore, then submits with a null CUDA stream | Standalone inference is a useful first product boundary; our native buffers and completion API can serve that purpose without claiming NvSci ABI compatibility | No NvSci adapter; physical native-API qualification pending |
| Hybrid registers CUDA allocations and submits into a CUDA stream | CUDA interop requires verified address mappings and ordering in both directions | Shared DRAM does not establish shared device virtual addresses; no CUDA-pointer registration or stream integration yet |
| Standalone waits on an EOF fence; hybrid synchronizes a CUDA stream | Return execution errors independently of successful submission and completion signaling | Native EXEC is synchronous and bounded; reset and safe reclamation are still unqualified |
| Statistics are extra registered outputs, interpreted by `etiTranslateStats` using a supplied clock frequency | Profiling needs a compatible payload decoder and a verified counter clock | Do not make NVIDIA-style layer statistics a prerequisite for first usable inference |
| Operator reconstruction uses ONNX Runtime for its reference comparisons | Reuse graph transformations only with separate compiler acceptance and N1X numerical tests | Passing the upstream Python tests alone cannot qualify DLA execution |

Source anchors: standalone [memory import](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsStandalone/main.cpp#L969),
[submission and wait](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsStandalone/main.cpp#L1159),
[statistics translation](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsStandalone/main.cpp#L1210),
hybrid [submission and stream synchronization](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/samples/cuDLA/cuDLALayerwiseStatsHybrid/main.cu#L680),
and [ONNX Runtime reference execution](https://github.com/NVIDIA/Deep-Learning-Accelerator-SW/blob/25a39b076c6b9143a25adbce8cb468b3bee821ee/operators/common/onnxruntime_utils.py).

For application partitioning, start with one contiguous supported subgraph
and explicit host transfers around it. The upstream FAQ identifies tensor
reformatting, small alternating GPU/DLA workloads and shared DRAM contention
as sources of overhead. Measure the complete pipeline against GPU-only
execution before attempting zero-copy or dividing a graph into many DLA
islands. Orin throughput and power-efficiency numbers are not N1X estimates.

The initial acceptance workload should progress from changing ReLU inputs to
a supported convolution and a small convolution/activation graph. Retain an
independent numerical reference, exact compiler/firmware versions and tensor
layouts for each artifact. Once those work repeatedly, choose an actual
application graph and measure correctness, end-to-end latency and memory
use. This is a smaller useful milestone than a full cuDLA replacement or an
ONNX Runtime execution provider.

The read-only machine check during this review found the P16 H7607BA running
`7.2.8-23-omarchy-p16-asus-npu2`. The loaded bootstrap lacks `enable_accel`,
`/dev/accel` is absent, and the linker cache has no `cudla`, `nvscibuf` or
`nvscisync` entries. These observations explain why the upstream samples
cannot currently serve as a working installation recipe on this boot. They
do not invalidate the earlier physical sysfs ReLU result or the on-disk
experimental accel implementation.

### Qualification sequence

The original-firmware retirement boundary is now tested for bypass and the
ReLU control path, including post-signal device-memory revocation; see
[bypass lifetime evidence](bypass-task.md#completion-boundary-and-repeated-firmware-submissions-2026-10-27)
and [ReLU retirement](model-probe.md#relu-firmware-retirement-boundary-2026-10-27).
The shared native executor in `driver/n1x_dla_job.c` now preserves a queue
across jobs, assigns unique completion values and blocks subsequent submissions
after uncertain execution errors. Both bootstrap probes use that executor.
The integrated firmware test exercises 32 jobs on the same native queue and
firmware instance. This is still an internal component: the sysfs probes remain
one-shot, physical repetition/reclamation is unqualified. An optional RAWIO-only
accel API and C/Python runtime now implement prepared-image submission and
result reading, with bounded retained allocations; their hardware qualification
remains open. The [native loader](loadable-runtime.md) now ingests NVDA 0.9.0
loadables with the recovered single-task DRAM contract; broader loadable
features and arbitrary-model compilation remain open.

1. **Reusable device lifecycle:** extend the proven startup path to repeated
   jobs, distinct completion IDs, bounded failures and demonstrably safe buffer
   release. Verify idle handling between tasks and client exit during a task.
   A completed fence alone does not establish whole-device DMA quiescence.
2. **Usable native runtime:** load supported artifacts outside the kernel,
   accept changing input tensors, expose tensor layouts and report actual output.
   Test repeated ReLU inputs against CPU references without rebooting.
3. **Broader compiler/engine coverage:** compile and validate a small convolution
   and then a multi-layer graph. Record exact supported tuples and compare
   numerical output, not merely ACK/fence state. Preserve explicit rejection of
   unsupported versions, shapes and operations.
4. **Interop and applications:** qualify shared GPU/DLA buffers and fences,
   then add cuDLA-facing adapters or framework integration where useful.

The native preemption/context serializers now have original-firmware control
flow coverage, but this does not move the physical lifetime gate. Our tests
show that preemption message `0x120b` and the preemption fence can occur even
when context saving fails. A future cancellation path must reconcile execution
errors, checkpoint state and task completion; neither that message nor a saved
checkpoint proves the engine has stopped accessing memory. See
[native runtime lifetime](native-runtime.md#lifetime-and-failures).

Full preemption, concurrent scheduling and CUDA integration are not prerequisites
for the first usable standalone release. Keep one serialized queue and bounded
synchronous execution initially. Qualify safe reuse after successful completion
and a bounded failure path that retains uncertain allocations and refuses new
work when necessary. Automatic recovery can follow once reset and DMA quiescence
are demonstrated. This keeps the implementation small without disguising the
current retained-memory experiment as a long-running inference runtime.

Layerwise statistics are valuable later, but the sample converts cycles using
a user-supplied MHz value. Its Orin BPMP path and example frequency are not N1X
settings. Our 430000-kHz HFRP request does not by itself calibrate every counter.
Expose raw counters and measured host timing until the counter clock and
profiling format are verified.

This plan builds on the real FP16 result without confusing a fixed diagnostic
with a general inference runtime. No new kernel, boot entry, firmware upload,
CUDA interception or system library replacement is required by this analysis.
