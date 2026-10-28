# Controlled shutdown and module reload

The bootstrap implements an explicit terminal shutdown for a healthy
firmware/device instance. **Hardware unload/reload qualification is pending.**
The first physical test on 2026-10-28 started firmware, passed the ReLU probe
and all eight cuDLA numerical jobs, then stopped at a host-side IRQ-mask
readback assertion before sending the HFRP power-off command. No memory was
freed and no unload/reload was attempted. See the
[attempt receipt](reload-attempt-2026-10-28.json).

That assertion has been corrected and the module rebuilt; the correction is
only offline-tested so far. Reboot the **same** `7.2.8-23-omarchy-p16-asus-npu2`
kernel before loading the new build, because the failed stop is terminal for
the currently loaded instance. No new kernel or boot entry is required.

## Recovered contract

The pinned Windows ARM64 KMD (SHA-256
`73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987`)
contains this original HAL-1 stop chain:

```text
d39de0 -> runtime vtable +1f8 -> 1423b0 -> primary vtable +278
        -> d447d0 -> HAL +3d8 (no-op for HAL 1) -> HAL +d0
        -> d4a4f0 -> outer +c8 / d405f0 (request 0x70)
        -> HFRP POWER_CONTROL 400, payload 43 00 on v1.5
```

With requested state 3, the wrapper also reaches `d44870`, which calls the stop
path again (the power policy suppresses already pending transitions), then
allocation teardown `d438b0`. The latter releases the retained configuration,
trace and task-related allocations. `scripts/inspect_kmd_shutdown.py` executes
the constructors, actual vtable thunks, stop wrappers and original power policy
in Unicorn. Transport, OS services and allocation teardown are modeled.
Ghidra exports in ignored `results/kmd-ghidra-stop-*.txt` corroborate the chain.

The Windows HAL returns success even after a power-command failure. Linux
does **not** copy that behavior. The Linux implementation requires the exact
two-byte immediate-off reply, then the independently requested `0x1af` power
state byte to equal 7. Original Windows wakeup consumes bits 0/1/2 as pending
clock/domain/rail gating. The query alone is not an engine-idle proof.

NVIDIA's [Linux power-control implementation](https://github.com/OE4T/linux-nv-oot/blob/33227347c89543042e0eda352db5de852983c8a3/drivers/video/tegra/host/nvdla/port/pm/nvdla_pm_hfrp_cmd_v1_3.c)
names the request bits MTCMOS OFF, RAIL OFF and CLOCK OFF. Immediate gating is
synchronous; delayed-off completion uses separate events. This driver sends
only the immediate bits, without guessed timings, voltages or reset values.

## Linux lifetime and ordering

1. Lock the accel device, waiting for any synchronous EXEC/REEXEC to finish.
   Mark it stopped before entering the backend; reject all future execution.
2. Acquire `boot_lock`, the same lock order used by submission. Block every
   startup/configuration sysfs writer. Shutdown is terminal for this instance.
3. Require healthy HFRP setup, supported board gating policy, a completed SROOT
   upload, successful previously attempted bootstrap phases and no active,
   poisoned or orphaned queue. Uncertain transactions cannot enter cleanup.
4. Drain only the recognized firmware idle notifications; never manufacture a
   completion or acknowledge unknown errors. Mask the owned DLA interrupt
   sources and read back the mask while the aperture is still powered.
   Preserve the raw values as diagnostics; do not require every bit of the
   raw route/mask intersection to become zero. The P16 retains bit 24, whose
   meaning is unknown; the original Windows KMD makes no such assertion.
   This polling backend never registers/enables Linux IRQ handlers, so there
   is no handler to synchronize or free.
5. Send immediate clock/domain/rail OFF and validate reply plus power-state
   query. No DLA-aperture register access occurs after power-off.
6. Free all bootstrap DMA (ReLU, bypass task, LPWR, region 5, config, ping and
   completed SROOT staging), then all retained accel allocations while its lock
   excludes READ, EXEC and close callbacks.
7. Balance both DLA runtime references, the SROOT runtime reference and both
   self-held module references. Open DRM clients retain their normal module
   references and must close before `rmmod` can succeed.
8. Unload normally; devres unregisters the accel node, unmaps MMIO and disables
   runtime PM. Load the module again and run the full startup and numerical
   inference sequence with fresh software state.

SROOT staging is released only on its completed synchronous upload path,
followed by firmware idle and any attempted DMA-ping qualification. Failed or
uncertain SROOT transactions remain quarantined. There is no command that
claims to cancel an in-flight secure upload.

Any stop failure preserves every DMA allocation, PM reference and module pin,
blocks further work, records the first error and refuses to repeat the stop.
A reboot remains required for these exceptional states. This is deliberate
error handling; `rmmod -f`, software counter resets and freeing potentially
active memory are not recovery. Automatic suspend/resume remains unsupported.
The platform shutdown callback uses the same checked path at system shutdown.

## Build and qualify

Use the pinned local inputs and environment from the existing bring-up guide:

```sh
make module cudla
venv/bin/python scripts/inspect_kmd_shutdown.py
venv/bin/python scripts/test_hfrp_stop.py
venv/bin/python scripts/test_linux_shutdown.py
venv/bin/python scripts/test_linux_accel.py
venv/bin/python scripts/test_linux_accel_backend.py
```

Offline results: original Windows stop dispatch reproduced; two supported
version encodings matched; 436 protocol failure/negative cases, shutdown
ownership/refcount/error cases and actual accel allocation cleanup passed.
These tests use modeled hardware and cannot replace the next command.

After rebooting to remove the instance with the failed stop (keep the current
power profile; the qualifier only records it):

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_p16_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

Run from the repository root. The destination must be new. This performs a
fresh startup when needed, eight real cuDLA numerical jobs, checked shutdown,
zero-retained-memory verification, rejection of EXEC after stop, refusal to
unload with an open client, successful unload after close, then fresh startup
and eight more numerical jobs. It repeats the cycle and leaves the final
working device running. Each operation is logged before execution; a failure
stops the sequence without an automatic retry. Only the final successful
receipt establishes hardware reload qualification. Kernel logs are saved
before the run, after each reload cycle and on a reported failure.

For a later manual stop after successful qualification:

```sh
sudo python3 scripts/npu_trigger.py shutdown
sudo cat /sys/bus/platform/devices/NVDA200A:00/shutdown_status
sudo rmmod n1x_npu_bootstrap
```

Require `error=0 hardware_off=1 released=1` before unload. A busy module means
clients or an unsuccessful shutdown still hold references; never force it.
