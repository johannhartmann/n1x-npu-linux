# Controlled Shutdown and Module Reload

## HFRP Power-Down Contract

[`driver/n1x_npu_boot_linux.c`](../driver/n1x_npu_boot_linux.c) implements the HAL-1 shutdown sequence reconstructed from `nvlddmkm.sys` (`0xd447d0` $\rightarrow$ `0xd4a4f0` $\rightarrow$ `0xd405f0`, request mask `0x70`):

1. **Quiesce `/dev/accel/accel0`**: [`n1x_dla_accel_stop()`](../driver/n1x_dla_accel.c) acquires `accel->lock`, marks `accel->stopped = true`, and blocks new submissions.
2. **Preflight Gate**: Requires healthy HFRP setup, gating mask `0x70`, completed `SROOT` upload, and no active or poisoned task queue (`-ENOTRECOVERABLE` otherwise).
3. **Idle Drain & Interrupt Mask**: Drains pending firmware idle messages, reads `0xd2c` (`owned`) and `0xd28` (`mask`), and masks owned interrupt bits via `0xd24` (`mask & owned`).
4. **HFRP Immediate Power-Off**: Sends `POWER_CONTROL` (`0x400`) payload `43 00` (MTCMOS OFF, RAIL OFF, CLOCK OFF), verifies the synchronous `43 00` reply, and queries power state `0x1af` to confirm the low three gating bits equal `0x07` ([`driver/n1x_hfrp_setup.c`](../driver/n1x_hfrp_setup.c)).
5. **DMA & Reference Release**: Frees bootstrap DMA allocations and unmapped accel job buffers, orphans any VMA-mapped job (`job->owner = NULL`, freed when the last VMA closes in `n1x_accel_vm_close()`), and drops runtime PM and module references.

If any shutdown step fails, `accel->stop_failed` latches fail-closed retention so DMA allocations, runtime PM, and module references remain pinned until reboot. The same checked stop/start sequence is used by `.remove()`, `.shutdown()`, and system sleep `.suspend()` / `.resume()`.

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

When `autostart=1` (default), `sudo modprobe -r n1x_npu_bootstrap` (or `sudo systemctl stop n1x-npu.service`) executes `n1x_boot_stop_device()` automatically during module unload.
