# Experimental N1X backend for NVIDIA cuDLA

The application links NVIDIA's original Linux `libcudla.so.1`. That frontend
loads this repository's `libnvcudla.so`, which parses supported N1X loadables,
stages CUDA tensors and submits them through the Linux N1X driver. The backend
does not calculate tensor outputs on the CPU.

This is an implementation of the [researched backend boundary](cudla-call-path.md),
not an official NVIDIA driver or complete cuDLA/TensorRT replacement. Physical
cuDLA execution passed on 2026-10-28: eight jobs through the full path matched
independent numerical references. See the result below.

## Build and local use

Acquire the hash-pinned cuDLA 13.4.92 package using the commands in
[the call-path investigation](cudla-call-path.md#new-executable-checks). The
installed CUDA toolkit supplies `cuda.h` and its link-time driver stub; the
running system must supply the actual `libcuda.so.1`.

```sh
make cudla
```

This produces `runtime/cudla/libnvcudla.so` and `runtime/cudla/cudla-smoke`.
The latter links the original NVIDIA library, not the backend directly.
Nothing is installed globally or substituted for NVIDIA's public library.

For an application launched from the repository root:

```sh
LD_LIBRARY_PATH="$PWD/runtime/cudla" \
  runtime/cudla/cudla-smoke MODEL.nvda INPUT.fp16 EXPECTED.fp16 NEW-OUTPUT.fp16
```

The current driver requires `CAP_SYS_RAWIO`; ordinary desktop users cannot
perform its INFO/EXEC/READ ioctls. Firmware startup is a separate, explicit
operation. The backend never loads a module, starts firmware or retries a
failed submission automatically. `N1X_DLA_DEVICE` optionally selects a device
path; otherwise discovery tries accel nodes and verifies the N1X driver name
and ABI through the existing C transport.

## Implemented contract and limits

- Hybrid device creation with the current CUDA context; standalone and shared
  static scratch flags are declined. Up to 16 device handles are supported.
- Native C++ NVDA loading for the same bounded single-task DRAM subset as the
  checked Python materializer. Input/output descriptions are sorted by dense
  bind IDs. Supported fixtures include ReLU, convolution and Conv -> ReLU.
- CUDA device-memory registration with context, bounds, alignment, managed
  memory and allocation-ID checks. Tokens are opaque, owner-specific and never
  reused during process lifetime. Freed/reallocated CUDA memory is rechecked
  before each task. UVA is reported unsupported.
- One task per submission, explicit CUDA stream, zero submission flags. Capture,
  external events and unsupported operations return an error before NPU work.
- **Submission blocks.** Input copies are enqueued on the given stream into a
  pinned host image, then synchronized before kernel EXEC. Results are read
  from the kernel, copied back on that CUDA stream and synchronized before
  returning success. This supplies ordered staging, not asynchronous overlap
  or zero-copy. Calls from CUDA host callbacks are not supported.
- Execution failures are sticky per device; a later submit returns the error
  without retry. Cleanup can release host-side registrations/modules while
  the kernel retains attempted DMA backing until checked explicit device stop.
  Uncertain execution/stop failures remain pinned until reboot.
- If CUDA synchronization fails, potentially referenced pinned host storage is
  retained until process exit. Such quarantine is capped at 64 MiB/128 entries
  globally. Completed transfers release their staging normally.
- The kernel’s 64-MiB/128-job retention limit remains. The new backend reuses a
  successful model’s allocation for subsequent I/O when the kernel advertises
  that capability. Reuse and checked shutdown/reclamation are implemented and
  offline-tested and physically qualified in two healthy P16 reload cycles
  plus changing-input and complete-image runs. Reset/recovery, asynchronous
  scheduling, CUDA Graphs, NvSci and private export-table interoperability are
  not implemented. The backend exports all required names and declines
  unsupported features; it does not create fake NvSci objects.

The module registry is capped at 64 models and 256 MiB of materialized images.
Descriptors and embedded instruction streams still require trusted input and
the kernel's RAWIO privilege; bounds-checked metadata is not an instruction
sandbox. General model compilation and TensorRT engine compatibility remain
separate from standalone N1X loadable execution through cuDLA.

## Evidence

`scripts/test_cudla_model.py` compiles the actual C++ materializer with UBSan
and compares its images, address ranges and public tensor descriptors against
the existing vendor-derived references. Nine valid and 65 malformed cases pass,
covering all three graph fixtures and their constant/intermediate storage.

`scripts/test_cudla_cuda_transport.py` uses the real NVIDIA frontend, our built
backend and the real installed CUDA driver. Its NPU transport is a temporary
mock that records submitted bytes and returns a fixed pattern, not an inference
implementation. Thirty-two scenarios pass, including all three graph serializers,
transport errors, wrong-reference rejection, ownership, capture rejection,
freed memory, sticky execution errors and injected CUDA copy/synchronization
failures. Injected synchronization failures drain the real CUDA operation before
reporting a synthetic error; they are not physical CUDA faults. This establishes CUDA/API transport
behavior but not physical DLA arithmetic.

```sh
make test-cudla
```

The accel fops now include `FOP_UNSIGNED_OFFSET`, required by this kernel's
`drm_open_helper`. The startup qualifier opens the node and checks VERSION/INFO
before any firmware trigger. The offline open test exercises the actual fops
initializer against the kernel's open gates, including the prior failure.

## Physical qualification

### Physical result: 2026-10-28

The real `cudla-smoke` application linked NVIDIA's unmodified Linux frontend,
loaded our backend and used real CUDA allocations. The backend passed the
materialized jobs to the actual Linux accel driver and extracted firmware.
All eight 3,136-byte outputs matched their independent reference files, and
all eight CUDA input allocations remained unchanged:

| Graph | Distinct inputs | Numerical results |
| --- | --- | --- |
| FP16 ReLU | 2 | 2 exact matches |
| FP16 1x1 convolution with channel bias | 3 | 3 exact matches |
| FP16 convolution followed by ReLU | 3 | 3 exact matches |

Kernel: `7.2.8-23-omarchy-p16-asus-npu2`; module srcversion
`A867C2D035B190C79E9E143`; boot ID
`81aeb5b0-5475-4a34-99db-a12c0195ba54`; performance profile.
The kernel reported zero retained accel jobs before the run and exactly eight
afterward, consuming 22,953,984 bytes. Its queue remained unpoisoned. Firmware
startup and the bypass probe completed first, and VERSION/INFO passed before
the firmware triggers.

The [tracked qualification receipt](cudla-hardware-2026-10-28.json) records the
frontend/backend/application/transport/firmware hashes and each model, input
and output hash. Full local records are under
`results/cudla-start-81aeb5b0.jsonl` and `results/cudla-hardware-81aeb5b0/`.
No mock transport or CUDA fault interposer was in the hardware application's
library search path. The only CPU arithmetic computed reference files; the
application obtained the compared results from CUDA buffers populated by DLA.

This qualifies this complete path for the three fixtures and eight jobs.
Blocking staging, kernel DMA retention, privileged access and the other limits
above remain in force.

### Reproduction

Use a fresh boot of `7.2.8-23-omarchy-p16-asus-npu2` with no bootstrap loaded.
The module is manually loaded from the build tree; no boot artifact changes
are required for this fops fix. After a firmware attempt, the old module cannot
be safely replaced in place.

```sh
make -C /usr/lib/modules/$(uname -r)/build M="$PWD/driver" n1x_npu_bootstrap.ko
pkexec /usr/bin/python3 "$PWD/scripts/qualify_p16_start.py" \
  --through bypass_task --enable-accel --log "$PWD/results/NEW-cudla-start.jsonl"
pkexec /usr/bin/python3 "$PWD/scripts/qualify_cudla.py" \
  --output-dir "$PWD/results/NEW-cudla-results"
```

The second qualifier runs eight independent application processes: two ReLU
inputs, three convolution inputs and three Conv -> ReLU inputs. Each process
uses the actual public cuDLA API, CUDA buffers and Linux driver. Independent
reference files are generated before execution, output is poisoned, inputs
are checked unchanged, and any failure stops the series without retry. A
successful final record must be present before claiming physical qualification.

`--prepare-only` generates the plan and references without CUDA or device
access. The current fixtures require 22,953,984 retained kernel bytes in total.
Result directories must be new; plans, attempt records, stdout/stderr and output
hashes are preserved, including on error. This does not qualify safe kernel
buffer reclamation, arbitrary graphs, suspend/resume or recovery.

### Completed-task reuse qualification

The default qualifier keeps the eight independent-process baseline jobs. Add
`--reuse` to execute the same eight distinct inputs with one loaded module per
graph, requiring exactly three new retained allocations and five REEXECs:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_cudla.py" \
  --reuse --output-dir "$PWD/results/NEW-cudla-reuse"
```

This requires a healthy initialized NPU with the reuse capability. The complete
reload qualifier now runs both baseline and reuse checks before the first stop
and after every restart. These checks passed in two physical P16 reload
cycles; see the [hardware receipt](npu-hardware-2026-10-28.json).
The production application's multi-input path has passed real-CUDA tests with
a modeled NPU peer, including changing-input snapshots and stopping on a wrong
reference without executing subsequent jobs. Those tests do not establish
physical DLA arithmetic or lifecycle behavior.
