# cuDLA to N1X firmware: the reusable frontend and missing Linux backend

Research date: 2026-10-28. This investigation reads vendor binaries and runs
isolated API tests; it does not start the device or change the installed driver.

Implementation follow-up: [experimental Linux backend](cudla-backend.md).
The findings below describe the research boundary; the linked document tracks
the subsequent implementation and distinguishes transport tests from hardware.

## Conclusion

Keep NVIDIA's public Linux `libcudla.so.1` and investigate implementing its
`libnvcudla.so` backend for N1X. This is a more concrete compatibility target
than replacing the public library or making a separate C/Python API the final
application interface. The existing Linux driver and loadable work supply
parts of that backend, not cuDLA compatibility by themselves.

This boundary is now experimentally established: NVIDIA's **actual Linux
SBSA cuDLA 13.4.92 frontend** loads our temporary mock `libnvcudla.so` and
forwards 17 publicly declared functions, preserving their arguments and the
backend's unsupported-operation result. All 18 backend export names match
the exports of the pinned Windows `nvcudla.dll`. This proves a loader/calling
boundary, not successful device creation, model loading or inference.

The inspected Windows driver **rejects `CUDLA_STANDALONE`** and its
`cudlaDrvGetNvSciSyncAttributes` immediately returns unsupported. Thus a
Jetson NvSci standalone recipe is not an accurate description of this Windows
implementation. Its CUDA integration must be investigated explicitly.

## Public sources and acquired inputs

- [NVIDIA CUDA 13.4.3 redistribution manifest](https://developer.download.nvidia.com/compute/cuda/redist/redistrib_13.4.3.json)
  lists cuDLA package 13.4.92 for Linux SBSA and Windows ARM64.
- [Linux SBSA package directory](https://developer.download.nvidia.com/compute/cuda/redist/libcudla/linux-sbsa/)
  provides the real library, a separate link-time stub, and headers. This
  investigation uses `lib/libcudla.so.1.0.0`, **not** `lib/stubs/libcudla.so`.
- [NVIDIA cuDLA API](https://docs.nvidia.com/cuda/cudla-api/cudla_api/group__CUDLA__API.html)
  defines the public lifecycle, CUDA memory registration, streams, external
  objects and asynchronous error reporting.
- [NVIDIA TensorRT DLA platform support](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/work-with-dla.html)
  still excludes Linux SBSA DLA in TensorRT 11.4. Availability of a cuDLA
  frontend package does not establish a supported N1X Linux platform backend.
- Microsoft's [SubmitCommand structure](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_submitcommand)
  separates the command GPU address/length from private driver metadata.
  [ValidateSubmitCommand](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmddi/ns-d3dkmddi-_dxgkarg_validatesubmitcommand)
  describes the kernel copy and expanded private-data allocation.

| Input | SHA-256 |
| --- | --- |
| Linux SBSA cuDLA 13.4.92 archive | `2842ab55ebcf92d3dafb8836f2952d84637ee7bec7072e49293aacdb44d0c7e6` |
| Actual Linux `libcudla.so.1.0.0` | `89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9` |
| Windows ARM64 cuDLA 13.4.92 archive | `b6e6882488013086f9fee6370ed59ed4231b46b41c0c9ec4c60161164a85963b` |
| Public Windows `cudla.dll` | `961751e6162df43ae982dc0676ff8af2ebf240445f2f707a981ab495574d1c63` |
| Windows 617.42 `nvcudla.dll` | `8da1b42d35aa79c71e3102f95dde30a75dbe498983d386b3cb68082f6b467ba0` |
| Windows 617.42 `nvdla_runtime.dll` | `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b` |
| Windows 617.42 `nvlddmkm.sys` | `73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987` |

Headers report public cuDLA API version 1.5.0. The package version, cuDLA API
version, HFRP protocol version and DLA loadable version are separate values.

## The Windows path has a callback back into cuDLA

The shorthand `cuDLA -> runtime -> KMD -> firmware` hides two libraries and a
callback boundary:

```text
Application / TensorRT
  -> cudla.dll                         public cudla* frontend
  -> nvcudla.dll                       cudlaDrv* backend, device/memory/stream handling
  -> nvdla_runtime.dll                 loadable, binding, command generation
  -> IKernelModeInterface in nvcudla.dll
  -> D3DKMT waits / SubmitCommand / signals
  -> Windows graphics kernel and nvlddmkm.sys
  -> outer task descriptor / mailbox / firmware / DLA engine
```

This is a composite of static binary evidence and individually executed
original-code slices. It is **not** a complete live Windows trace or a single
unbroken emulator run from the public API through the physical engine.

The public Windows `cudla.dll` contains the same 18 `cudlaDrv*` resolver names
and `nvcudla.dll` loader name. `nvcudla.dll` delay-imports runtime functions
including `createDevice(IKernelModeInterface*)`, `createRuntime`, `init2`,
`createMemoryMap`, `bindSubmitEvent` and `IRuntime::submit`. Passing a KMI
object to the runtime explains why submission returns into cuDLA for OS calls.

It also delay-imports **`nvcuextend.dll`** with:

- `cuExtendAddClientToCUDAContext`
- `cuExtendRemoveClientFromCUDAContext`
- `cuExtendStreamOps`
- `cuExtendGetAttributeInfo`

These imports identify a private CUDA integration surface. They do not prove
that every submit calls all four functions. That DLL is absent from the
currently inspected `sources/nvidia/Display.Driver` directory. Public CUDA
stream APIs cannot simply be assumed to replace those semantics.

## Concrete anchors and their evidence level

All Windows addresses below are RVAs in the hash-pinned binaries.

| Boundary | Anchor | Evidence and remaining limit |
| --- | --- | --- |
| Public Linux frontend -> backend | `loadDriver` ELF VA `0x1f80`; resolver `0x1cf0` | Original native library loads `libnvcudla.so`, resolves 18 functions; 17 typed calls tested against a mock |
| Windows device creation | cuDLA `0xcfb8` | Original instructions reject flags 1 and 3 with status 13; initialization/locks/allocator modeled |
| Windows public driver submit | cuDLA `0xd9c8` | Static dispatch through context vtable slot `+0x28`; two constructed factory tables select `0x13c58` or `0x21f88`; full public submit path still unexecuted |
| Runtime -> KMI | runtime `0x75858`, builder `0x743a8`; cuDLA KMI table `0xc2078` | Original combined runtime/envelope tests; device objects and OS calls modeled |
| KMI -> Windows | cuDLA `0x97c0`, `0x9f78`, `0x9938` | Wait, submit, signal envelopes; resolver `0xb8b8` executed |
| UMD metadata -> KMD job | KMD `0x19b1500 -> 0xdca010 -> 0xdd3130 -> 0xdd6188 -> 0xdd28f8` | Actual runtime private bytes transferred into modeled OS arguments; original translation and job production executed |
| KMD -> firmware task | KMD builder `0xdd7460`; submit `0xdd6888 -> 0xdd22c8 -> 0xd45050 -> 0xd4a400 -> 0xd49fc0` | Original producers and firmware consumers executed in component tests; OS scheduling and low-level sender boundaries remain modeled |
| Firmware -> completion | Original HAL-1 scheduler, postactions, engine IRQ paths | Emulated engine events; fixed sysfs ReLU separately verified physically; native accel inference remains unqualified |

The runtime supplies an inner version-2, 64-byte command and an independent
852-byte private-data envelope. The KMD expands metadata to 1,424 bytes and
builds an outer version-3 descriptor, size `0x500`, engine `0x44`. Firmware
consumes both descriptor levels. A Linux cuDLA backend need not reproduce the
Windows metadata envelope if it preserves the verified task, memory and
synchronization contracts using a native transport.

Detailed existing evidence:
[runtime and WDDM submission](windows-submission.md),
[KMD descriptors and firmware consumption](windows-kmd-task.md),
[memory release](windows-buffer-lifetime.md).

## New executable checks

`scripts/inspect_cudla_frontend.py` establishes:

1. All 17 functions declared in the acquired public headers reach the
   corresponding temporary `cudlaDrv*` function with identical scalar/pointer
   argument values. The mock returns `cudlaErrorUnsupportedOperation`, which
   the actual public frontend propagates. Pointer arguments are deliberately
   inert tokens; no buffer or model operation is performed.
2. Omitting each of the 18 driver exports in turn prevents initialization.
   The observed `cudlaGetVersion` return is **1**, not success; the frontend
   therefore requires even currently unsupported exports to exist. Missing
   symbols must not be confused with a usable partial backend.
3. The private `cudlaDrvGetExportTable` name is required by the resolver but
   lacks a declaration in these public headers. Its test stub aborts if called;
   its prototype and table layout are **not** claimed as recovered.
4. Original Windows `cudlaDrvCreateDevice` returns status 13 for flags 1 and
   3, leaves the output handle unchanged, and never reaches runtime creation
   or D3DKMT. Flag 4 returns invalid parameter. The helper models successful
   prior Windows initialization, locks and a successful handle allocation.
   Hybrid creation with flag 0 is not covered by this test.
5. Original Windows `cudlaDrvGetNvSciSyncAttributes` returns 13 immediately.

The ELF was loaded only in subprocesses with a temporary backend search path.
Nothing was installed into the system library path. Proprietary inputs and
generated results remain ignored; the repository contains only the test and
research description.

The existing original-code runtime-command suite (39 cases) and KMD-descriptor
suite (32 cases) were also rerun successfully during this investigation. Their
modeled OS, DMA and engine boundaries remain as documented; these passes do
not turn the composite call graph into a physical end-to-end test.

To acquire the Linux input, run from the repository root:

```sh
mkdir -p sources/cudla-13.4.92
curl -fL https://developer.download.nvidia.com/compute/cuda/redist/libcudla/linux-sbsa/libcudla-linux-sbsa-13.4.92-archive.tar.xz \
  -o sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive.tar.xz
printf '%s\n' '2842ab55ebcf92d3dafb8836f2952d84637ee7bec7072e49293aacdb44d0c7e6  sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive.tar.xz' | sha256sum -c -
```

After the hash check succeeds, extract and test on ARM64 with the existing
hash-pinned Windows driver input:

```sh
tar -xJf sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive.tar.xz -C sources/cudla-13.4.92
venv/bin/python scripts/inspect_cudla_frontend.py > results/cudla-frontend-boundary.json
```

## Implementation route

The target architecture is:

```text
Application using the real public cuDLA API
  -> NVIDIA libcudla.so.1
  -> N1X Linux libnvcudla.so backend
       device/module/tensor/buffer/task/error semantics
       native loadable implementation OR a qualified reusable NVIDIA ELF runtime
  -> N1X Linux kernel driver, potentially through /dev/accel/accelN
  -> extracted N1X firmware and physical DLA
```

`/dev/accel` is an internal transport choice here, not a competing application
API. It does not make the original vendor backend work automatically. Do not
install the temporary mock or advertise it as an implementation.

Proceed in this order:

1. **Finish the Windows frontend-to-runtime trace.** Start at create `0xcfb8`,
   module load `0xd5f8`, memory register `0xd498` and submit `0xd9c8`. Follow the
   actual device factory/vtables, `IRuntime` binding and submit calls. Replace
   the currently constructed objects with original factory output where
   feasible. Resolve the private export table and CUDA extension callbacks.
   Use the existing Unicorn harness for differential execution; use radare2
   and Ghidra for indirect calls, layouts and cross-references.
2. **Check reuse below the public frontend before rewriting the runtime.**
   A native `libnvdla_runtime.so` with the required N1X target/format and an
   injectable KMI could preserve NVIDIA's loadable logic. Its existence in
   Jetson packages alone is insufficient: verify target selection, format
   gates, exported C++ ABI, dependencies and OS callbacks. No compatible N1X
   runtime ELF was established in this investigation. Running a Windows DLL
   under Linux would additionally require its loader/ABI/OS environment.
3. **Build the backend with honest capabilities.** Implement discovery,
   device/module objects and tensor attributes through the real frontend.
   Export all required names, but return unsupported for unimplemented
   operations. These smoke tests are a milestone, not inference support.
4. **Implement the intended CUDA contract.** For a Windows-equivalent hybrid
   path, establish CUDA allocation ownership/mapping and stream ordering in
   both directions, including asynchronous errors and release. Shared DRAM
   does not make a CUDA virtual pointer a DLA IOVA. Explicit host staging may
   be a first strategy if correct stream synchronization can be demonstrated;
   do not silently reinterpret a CUDA pointer as a host allocation. Standalone
   NvSci support would be a separate feature, not inherited from this driver.
5. **Qualify the complete Linux path.** First remedy and preflight the existing
   accel open failure before firmware startup, then execute real cuDLA
   lifecycle calls with changing inputs and independent numerical checks.
   Cover completion/error propagation, safe reuse/release and repeated work.
   The current retained-DMA limits and unqualified recovery prevent treating
   this as a long-running inference backend today.

This route preserves the actual NVIDIA public library where reuse has been
tested. It avoids promising full TensorRT compatibility from matching function
names: engine compilation/target support, hidden export tables and CUDA
integration require separate evidence.
