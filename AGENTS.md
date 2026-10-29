# Repository Guidelines

## Layout

`driver/` and `include/` contain the kernel components and UAPI. `runtime/`
contains the C transport, cuDLA backend and Python bindings. `demo/` is the
complete-image example; its published images have recorded provenance in
`demo/assets/README.md`. `scripts/` contains operational tools, offline research
and Python regression harnesses; `tests/` holds standalone C/VM fixtures.
Keep Python module filenames and import relationships stable when reorganizing
research tooling.

Current user instructions are in `docs/`. `docs/research/` contains detailed,
dated investigations. `docs/evidence/` contains immutable qualification receipts.
Ignored `sources/`, `results/` and `venv/` are local inputs and outputs, not
disposable scratch directories: previous hardware evidence may live there.

## Development and verification

Read `CONTRIBUTING.md` for dependencies and the test matrix. Run `make help`
for targets. `make check` combines repository consistency checks and portable
protocol/lifetime tests; it needs the analysis environment in `venv/` or an
explicit `PYTHON=/path/to/python`. These checks need no vendor files or device.

`make module` only compiles against `KERNEL_RELEASE` (the running kernel by
default). `make runtime cudla` builds userspace with the acquired cuDLA headers
and CUDA toolkit. Vendor-backed suites have extra fixtures; `make test-cudla`
uses actual CUDA with a modeled NPU. Do not present these as hardware inference.

## Change boundaries

Follow existing local C/C++ and Python conventions. Keep changes focused and
cover meaningful protocol or lifetime changes with regression tests. Preserve
exact board/BIOS/kernel and input-hash guards. Never infer writable register
values, physical quiescence or successful recovery from an emulator fixture.

Routine cleanup and tests must not change boot entries, load/unload modules,
start firmware, alter the power profile or delete local evidence. Hardware
work requires the explicitly requested operation and its documented sequence;
uncertain failures retain DMA and lifetime references without forced unload.

Use English for maintained documentation. Preserve measured receipts; update
current status with a new receipt instead of rewriting historical results.
Do not commit vendor inputs, generated binaries or local credentials.
