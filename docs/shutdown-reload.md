# Controlled Shutdown and Module Reload

## HFRP Power-Down Contract

[`driver/n1x_npu_boot_linux.c`](../driver/n1x_npu_boot_linux.c) implements the HAL-1 and HFRP shutdown sequence (request mask `0x70`):

1. **Quiesce `/dev/accel/accel0`**: [`n1x_dla_accel_stop()`](../driver/n1x_dla_accel.c) acquires `accel->lock`, blocks new submissions, and invokes [`n1x_boot_stop_hardware()`](../driver/n1x_npu_boot_linux.c).
2. **Preflight Gate**: Requires healthy HFRP setup, gating mask `0x70`, completed `SROOT` upload, and no active or poisoned task queue (`-ENOTRECOVERABLE` otherwise).
3. **Idle Drain & Interrupt Mask**: Drains pending firmware idle messages, reads `0xd2c` (`owned`) and `0xd28` (`mask`), and masks owned interrupt bits via `0xd24` (`mask & owned`).
4. **HFRP Immediate Power-Off**: [`n1x_hfrp_stop()`](../driver/n1x_hfrp.c) sends HFRP command `400` (`0x190`) with payload `43 00` (clock, power domain, and rail OFF), verifies the synchronous `43 00` reply, and queries power state `0x1af` (`431`) to confirm `(state.raw & 0x07) == 0x07`.
5. **DMA & Reference Release**: Frees bootstrap DMA allocations and unmapped accel job buffers, marks `accel->stopped = true` (`accel->stop_failed = false`), orphans any VMA-mapped job (`job->owner = NULL`, freed when the last VMA closes in `n1x_accel_vm_close()`), and drops runtime PM and module references.

If any shutdown step fails, `accel->stop_failed` is set to `true` (and `boot_shutdown.error` latches the error) so DMA allocations, runtime PM, and module references remain pinned until reboot. The same checked stop/start sequence is used by `.remove()`, `.shutdown()`, and system sleep `.suspend()` / `.resume()`.

## Build and Qualify

Published [DSP client leases](sroot-dsp-lifetime.md) prevent this shutdown
sequence with `EBUSY`, before accelerator or power changes. Their guard is
independent of whether temporary image staging remains allocated. A verified
DSP core shutdown and provider-lease release contract is still required before
the shared NPU/SROOT provider may shut down in that state.

Run the offline shutdown and transport regression suite:

```sh
make test-portable
```

Run the physical two-cycle stop/unload/reload qualification:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_npu_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

Published firmware retains module/runtime-PM leases after successful startup
and resume. Request checked shutdown **before** unloading; `remove()` cannot
cancel a module unload after a failed hardware stop. Manual sysfs bind/unbind
attributes are suppressed for the same reason. A failed stop retains its
leases and must not be followed by forced module removal.

The packaged `n1x-npu.service` uses
[`n1x-npu-stop`](../packaging/systemd/n1x-npu-stop), which locates the bound DLA
shutdown attribute, writes `stop`, and attempts `modprobe -r` only after that
write succeeds. It requires neither debugfs nor a particular ACPI instance
number. Active client file descriptors can still prevent module removal.

## System Sleep

The aggregate's DLA-to-SROOT device link makes DLA suspend before SROOT and
SROOT resume before DLA restarts firmware. Component matching alone provides
no such PM ordering; see the kernel's [component helper documentation](https://docs.kernel.org/driver-api/component.html)
and [device links](https://docs.kernel.org/driver-api/device_link.html).
The stateless link is explicitly owned by the aggregate, not by devres, and
is deleted when its bound components are released. It does not replace the
published firmware's runtime-PM references or the DSP lease guard.

PM `prepare` checks known unsafe stop conditions and published DSP leases
before any device is suspended. It serializes with submissions and secure DSP
uploads. New hardware operations return `EBUSY` until `complete`, including
after an aborted sleep transition. The existing checked stop/start path runs
during suspend/resume. This admission gate does not make a failed firmware
shutdown recoverable or establish physical suspend/resume qualification.
