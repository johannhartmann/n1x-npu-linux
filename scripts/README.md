# Tool index

Run commands from the repository root. Most tools depend on other Python
modules in this directory, so paths are kept stable. The collection includes
reproduction code and regression evidence, not a batch installer.

## Main workflow

| Command/tool | Purpose | Requirements and effects |
| --- | --- | --- |
| `check_repo.py` | Check local documentation links and sections, Python syntax, JSON and published image hashes | Standard library only; no project imports or hardware access |
| `prepare_inputs.py` | Verify pinned inputs | Read-only file hashing by default |
| `prepare_inputs.py --fetch` | Acquire public libraries/model/sample | Network; writes ignored inputs; no system install |
| `prepare_inputs.py --extract-firmware` | Extract firmware from supplied KMD | Analysis venv; writes ignored inputs; no device access |
| `build_models.py --superres` | Reconstruct four checked loadables | ARM64, vendor inputs, bwrap/GDB/Unicorn; device-free; fresh outputs required |
| `qualify_p16_start.py` | Explicit bounded firmware-startup prefix | Root, exact board/kernel, ROM and firmware; changes NPU state |
| `qualify_cudla.py` | Eight physical numerical jobs; `--reuse` keeps each graph loaded and verifies one allocation per graph | Already initialized NPU and CUDA; root; `--prepare-only` only creates references |
| `qualify_p16_reload.py` | Stop, cleanup, unload, reload and numerical jobs | Root, exact audited setup; two healthy physical cycles verified on P16 |
| `qualify_superres_tile.py` | One physical learned-graph window | Root, initialized NPU, CUDA, image/reference venv |
| `npu_trigger.py` | One unbuffered sysfs operation | Root for real hardware paths; no retries |
| `select_npu_dma_domains.py` | Inspect dedicated IOMMU groups | Default read-only; `--apply` changes only audited unbound groups |
| `../demo-superres` | Full-image super-resolution launcher | Explicit RAWIO elevation; 512/1024 image runs and reuse verified on P16 |

Use [setup](../docs/setup.md) for ordering and [status](../docs/status.md) for
which stages have physical evidence. None of the input/model helpers starts
firmware or sets a power profile. Hardware qualifiers record the selected profile and leave it unchanged.

## Test targets

| Target | Scope | Additional inputs |
| --- | --- | --- |
| `make check` | Repository consistency plus the portable tests used by CI | Analysis venv and C compiler; no GPU/vendor files |
| `make check-repo` | Local links/sections, Python syntax, JSON and published image hashes | Python standard library only |
| `make test` | Native C firmware parser/bounds tests | C compiler only |
| `make test-portable` | Fixture-free channel, shutdown, ownership and controller checks | Analysis venv and C compiler; no GPU/vendor files |
| `make test-p16-start` | Board/startup/runtime regression suite | Pinned KMD/firmware, compiled fixtures, BIOS capture and analysis inputs |
| `make test-p16-stop` | Original stop/wakeup replay, joined protocol/lifetime faults and cleanup tests | KMD, firmware and compiled ReLU fixture; no live NPU |
| `make test-cudla` | Backend model/API/CUDA staging checks | cuDLA package, CUDA headers and actual CUDA driver; NPU transport is modeled |
| `make test-relu`, `test-conv`, `test-conv-relu` | Fixture compilation/materialization and protocol checks | Exact vendor inputs and analysis artifacts |
| `make test-vendor` | Broad historical original-code regression suite | Additional pinned auxiliary drivers, runtime libraries, ACPI files and artifacts |
| `make module`, `make kernel-check` | Kernel compilation only | Matching installed kernel headers |

Offline means no NPU device submission; some tests still use actual CUDA or
vendor code in isolated/emulated execution. Read each target’s requirements.
Individual `test_*.py` files are executable regression tests, not all runnable
from a source-only checkout. CI runs `make check`, including the fixture-free subset and repository checks.
`make help` is the default target. All Python targets accept `PYTHON=/path/to/python`.

## Research tool families

- `inspect_*.py`: original ABI, compiler, KMD, allocator and format investigations.
  Most execute pinned original instructions in Unicorn and need vendor inputs.
- `emulate_*.py`: firmware/transport paths against modeled MMIO, DMA and OS edges.
- `test_*.py`, `test_cudla_api.cpp`: negative cases, original/native comparisons,
  mocked Linux boundaries and specifically labeled physical/API prerequisites.
- `probe_native_graph.py`, `capture_native_*.gdb`, `package_windows_output.gdb`:
  the native sandbox/compiler bridge used by `build_models.py`.
- `create_*_probe.py`, `prepare_p16_relu.py`, `verify_*_loadable.py`:
  deterministic model inputs, numerical oracles and artifact validation.
- `extract_*.py`, `fetch_windows_compiler.py`: file-only acquisition and extraction.
- `collect_acpi.py`, `audit_acpi_namespace.py`, `inspect_iort.py`, `inventory.py`:
  firmware namespace/resource inspection. `inventory.py --dsdt FILE` uses a
  caller-supplied disassembly and reads live ACPI metadata.
- `read_*.c`, `read_*.cpp`, `read_*.py`, `capture_gpu_iova_frts.py`: specialized
  hardware readout. Some require root, driver APIs or exact SDK versions;
  consult their dedicated documents before use.
- `ghidra/ExportFunctions.java`, `arm64_*.py`, `msvc_vtables.py`,
  `riscv_string_refs.py`, `pe_resources.py`, `indirect_slots.py`: reusable
  static-analysis and emulation support.

## Historical kernel and boot tools

`build_*kernel.py`, `build_*uki.py`, `package_sroot_kernel.py`,
`run_sroot_mkinitcpio.py`, `prepare_*capture.py`, `prepare_frts_diagnostic.py`,
`install_*.py` and corresponding audit/verifier tests implement earlier
isolated FRTS, logging and SROOT kernel experiments. Build helpers generate
local artifacts; install helpers can modify privileged files and boot menus.
They are not part of normal demo startup. Follow [boot logging](../docs/research/boot-logging.md),
[Linux backend](../docs/research/linux-backend.md) or [FRTS capture](../docs/research/npu2-frts-capture.md)
for the specific experiment and recovery procedure.
