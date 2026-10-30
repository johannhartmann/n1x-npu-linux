# Security

## Scope

This is an experimental driver for the exact P16/BIOS/kernel combination in
[the support status](docs/status.md). The current `master` branch is maintained;
there is no stable release or production support commitment.

The transport requires `CAP_SYS_RAWIO` and accepts **trusted models only**.
Metadata bounds checks do not sandbox the embedded DLA instruction stream.
The public demos keep Python, image parsing, ONNX validation and output files
unprivileged; only the installed root-owned broker holds RAWIO after changing
to the dedicated `n1x-dla` system identity. Do not expose
the device, broker protocol or administrative qualification tools to untrusted
tenants.
The broker re-arms and verifies its parent-death signal after changing
credentials, applies a 60-second protocol-I/O deadline, rejects duplicate model
IDs and bounds aggregate model/tensor memory. Before accepting requests it also
materializes every selected model and verifies that the complete session fits
the kernel's remaining retained-DMA and job budgets. It never partially submits
a session that cannot fit.
The accel node is root-only and the driver independently rejects opens and
ioctls without `CAP_SYS_RAWIO`. ABI 2 also limits completed-job patches to the
writable ranges registered at initial submission; the standard backend
registers only tensor inputs and outputs. A RAWIO caller is still trusted to
choose those ranges and to supply a safe instruction stream.
Successful jobs remain retained across broker processes because the recovered
completion protocol is not proof of DMA quiescence. Only the checked
administrative device-stop path may reclaim them. This is deliberate: do not
replace it with per-job freeing based only on a fence or idle notification.
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
