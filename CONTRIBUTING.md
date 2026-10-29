# Contributing

Contributions to the Linux driver, cuDLA backend, compiler bridge, tests and
documentation are welcome. Start with [the supported platform and measured
results](docs/status.md). Other N1X systems are research targets, not verified
ports; the current board, BIOS and kernel guards are intentional.

## Work locally

For the portable development checks, any Linux host with a C compiler, make,
Python 3.12 and Git is sufficient. No NVIDIA hardware or vendor binaries are
required:

```sh
python3.12 -m venv venv
venv/bin/python -m pip install -r requirements.txt
make check
```

`make check` validates local links and section anchors, Python syntax, JSON
receipts and published image hashes, then runs the same portable tests as CI.
Use `make help` for other targets. To use an existing Python environment,
set `PYTHON=/path/to/python`. Native compiler work additionally requires an
ARM64 host and the [pinned inputs and dependencies](docs/setup.md).

| Change | Relevant verification |
| --- | --- |
| Documentation, layout, examples | `make check-repo` |
| Protocol, ownership, shutdown, controller code | `make test-portable` |
| Kernel integration | `make module` with matching audited kernel headers |
| Original-driver comparisons | `make test-p16-start`, `make test-p16-stop` with their pinned fixtures |
| cuDLA loader/backend | `make test-cudla` with the CUDA driver and acquired cuDLA package; NPU transport is modeled |
| Image preparation and stitching | `sources/superres-venv/bin/python scripts/test_superres_image.py` |

Builds and portable tests never install a module or start NPU firmware. Real
hardware qualification is a separate manual workflow described in
[shutdown/reload](docs/shutdown-reload.md). Preserve the selected power profile
and earlier evidence. Do not force-unload a module or retry an uncertain
transaction to get a passing result.

## Keep changes reviewable

- Follow the surrounding C/C++ or Python style; there is no mandatory formatter.
- Add regression coverage for changed protocol, lifetime or allocation behavior.
  Preserve failure injection and the distinction between modeled and physical I/O.
- Record exact board/BIOS/kernel, artifact hashes and execution mode when making
  hardware claims. A build, emulator trace or device node does not prove inference.
- Keep current instructions in `docs/`; detailed investigations belong in
  `docs/research/`. Measured receipts belong in `docs/evidence/` and should retain
  their original contents. Add a new receipt when the evidence changes.
- Keep vendor binaries, firmware, ROM captures, generated loadables, environments
  and local logs out of Git. See [licensing](LICENSING.md) and [NOTICE](NOTICE).

Open a pull request describing the problem, resulting behavior and checks run.
Include hardware limitations or checks that remain unperformed. Recent commits
use short imperative subjects; there is no required commit-message prefix.

For a reproducible bug, include the command, full error and relevant logs using
the [issue template](https://github.com/johannhartmann/n1x-npu-linux/issues/new/choose).
For a potential vulnerability, follow [SECURITY.md](SECURITY.md).
