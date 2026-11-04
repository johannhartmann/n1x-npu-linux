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

Run the offline shutdown and transport regression suite:

```sh
make test-portable
```

Run the physical two-cycle stop/unload/reload qualification:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_npu_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

`sudo modprobe -r n1x_npu_bootstrap` (or `sudo systemctl stop n1x-npu.service`) executes `n1x_boot_stop_device()` automatically during module unload.
