# Security

## Scope

This is an experimental driver for the exact P16/BIOS/kernel combination in
[the support status](docs/status.md). The current `master` branch is maintained;
there is no stable release or production support commitment.

The transport requires `CAP_SYS_RAWIO` and accepts **trusted models only**.
Metadata bounds checks do not sandbox the embedded DLA instruction stream.
Do not expose the device or privileged demo launcher to untrusted tenants.
The setup tools acquire vendor inputs by pinned hashes; their distribution
terms remain separate from this repository's source licenses.

Report unexpected access without the required capability, incorrect lifetime
or DMA isolation, unsafe input handling, or bypasses of the intended guards.
Documented privileged access is not a claim that all privileged inputs are safe.

## Reporting

Use GitHub's **Security → Report a vulnerability** on this repository:
[private vulnerability report](https://github.com/johannhartmann/n1x-npu-linux/security/advisories/new).

Include the affected commit, board/BIOS/kernel, reproduction steps and the
observed impact. State whether the behavior is static analysis, emulation,
a modeled test or physical hardware execution. Avoid sharing credentials,
private data or vendor binaries in a report; hashes and minimal traces are
usually sufficient. Do not publish a working exploit in a general issue.

Ordinary setup failures, unsupported hardware and numerical mismatches can
use the [bug report form](https://github.com/johannhartmann/n1x-npu-linux/issues/new/choose).
