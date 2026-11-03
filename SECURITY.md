# Security

## Scope

This is an experimental driver for the exact P16/BIOS/kernel combination in
[the support status](docs/status.md). The current `master` branch is maintained;
there is no stable release or production support commitment.

By default (`require_rawio=0`), `/dev/accel/accel0` uses standard VFS permissions
(`0660 root:render`, `TAG+="uaccess"` via [`99-n1x-npu.rules`](packaging/udev/99-n1x-npu.rules));
passing `require_rawio=1` restricts device open/ioctl to `CAP_SYS_RAWIO` (and the
optional `install_cudla_broker.py` workflow installs `config/99-n1x-dla.rules`
with `0600 root:root` for the confined `n1x-dla` broker). In all modes, the driver
accepts **trusted models only**: metadata bounds checks do not sandbox the embedded
DLA instruction stream. Do not expose the device, broker protocol or administrative
qualification tools to untrusted tenants.
The broker re-arms and verifies its parent-death signal after changing
credentials, applies a 60-second protocol-I/O deadline, rejects duplicate model
IDs and bounds aggregate model/tensor memory. Before accepting requests it also
materializes every selected model and verifies that the complete session fits
the kernel's remaining retained-DMA and job budgets. It never partially submits
a session that cannot fit.
ABI 2 limits completed-job `REEXEC` patches to the writable ranges registered at
initial submission (and blocks `REEXEC` with `-EBUSY` while a job is mapped read-only
via `mmap()`). Completed jobs owned by an open `/dev/accel/accel0` file descriptor
are reclaimed automatically in `postclose()` when the file descriptor closes (and
when all VMAs close in `n1x_accel_vm_close()`), while any job that fails or times
out — or any instance whose hardware stop fails (`accel->stop_failed`) — remains
retained in DMA memory until confirmed hardware shutdown (`43 00` / state `07`)
or reboot.
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
