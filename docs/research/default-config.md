# Windows-default configuration preparation and import

> Historical research record. This document preserves observations from earlier
> bring-up stages, including superseded status claims and commands for older builds.
> Use [current status](../status.md) and [setup](../setup.md) for the tested behavior
> and current procedure. Command paths below are relative to the repository root.

The optional Linux bootstrap can now prepare and import the exported HAL-1
configuration using the observed Windows constructor defaults. **It has not
been run on hardware.** It does not expose arbitrary user settings or effective
Windows override-table entries, and a matched import reply does not establish
full initialization or inference.

## What Windows changes

`test_default_config.py` executes the full original KMD modifier at RVA
`0xd47830` against the pinned NVIDIA 617.42 binary. The OS getter is a synthetic
boundary; the entire payload transformation executes as original ARM64 code.

The function requires nonzero CPU/DMA addresses at context `+0x4f8/+0x500`.
With either missing it returns `0x40` without changing the buffer. Optional
context `+0x640` is a flag/value override table. Tests cover both a null table
and an allocated all-disabled table; **effective custom overrides are not
ported by this default helper**.

Even with no effective overrides, the constructor's default state makes two
changes:

| Context condition | Payload byte offset | Value |
|---|---:|---:|
| byte `+0x47e` nonzero (default 1) | 1 | 2 |
| word `+0x480` nonzero (default 1) | `0x32` = 50 | 1 |

All other bytes remain unchanged. Byte zero is preserved. No names, operating
units or guessed physical meanings are assigned to these fields. The same
context word `+0x480` also enables the original region-5 allocation, so the
Linux import phase requires successful region-5 registration first.

`n1x_dla_prepare_default_config` applies exactly these two byte assignments in
place to an already exported buffer. It requires at least 252 bytes, validates
before modification and preserves all remaining/trailing bytes. It does not
allocate, submit a command or claim that a CPU buffer is DMA-visible.

## Firmware round trip

The test runs **22 → 21 → 22 in the same raw HAL-1 firmware instance**, after
original reset and through original trap, DMA and ACK paths. Its actual first
export is transformed by the Windows modifier and compared byte-for-byte with
the C helper's output. That payload is then imported; host contents are erased
before the third command exports again. The final 252 bytes match the prepared
payload, differing from the original factory export only at offsets 1 and 50.
The factory mode byte is 2. All three original trap returns occur and the 189
four-byte DMA operations execute without firmware function stubs or code edits.

This is not a live round trip and does not establish opaque SROOT-image code
identity, physical coherency or behavior for every factory/override mode. Other
raw HAL-1 config modes have separately observed firmware normalization behavior;
the helper itself preserves input bytes except the two default assignments.

## Linux bootstrap phase

`enable_config_apply=1` must be selected at module load. The root/CAP_SYS_RAWIO
`apply_config` attribute accepts `apply`; it does not run automatically. Its
prerequisites are the exact test kernel/machine, both bound endpoints, this
boot's verified DMA-word ping, successful region-5 registration, and a valid
prior configuration export with retained backing.

The phase revalidates DLA's DMA gate and the entire retained 4096-byte extent
against the 36-bit mask. It reuses that DLA-owned coherent allocation instead
of replacing/freeing storage already published to firmware. After preparation
and dma_wmb it submits command 21 through the checked control transaction.
Only a matched zero-status reply sets `config_apply_accepted`. All outcomes
retain the allocation; one attempted transaction blocks repetition. Preflight
errors do not mutate the buffer. A stale-response error after preparation may
leave the two CPU bytes changed, but does not prove firmware imported them.

The existing `config_snapshot` output remains the saved **pre-import export**.
It is not silently relabeled as applied/read-back state. The native phase does
not perform a second physical export or verify persistence: acceptance, default
payload construction and actual applied/read-back state are separate claims.
The same-instance round trip above supplies software evidence only. Status
preserves import attempt/error/acceptance and raw reply validity/header.

## Reproduction and pending operation

```sh
venv/bin/python scripts/test_default_config.py
venv/bin/python scripts/test_linux_config_apply.py
```

Validation includes six Windows/C payload comparisons (three byte patterns,
null versus all-disabled table), invalid CPU/DMA/short-buffer controls, the
same-instance raw-firmware round trip, and 27 exact Linux phase lifecycle cases
linked to the real prepare/control/sender/ACK pipeline. All ten I/O failures,
missing prerequisites, DMA extent, stale/rejected/mismatched responses, no
repeat attempt and preservation of the saved export/backing are checked.
Affected upload, ping, region, snapshot, sender, command-23 and control tests
also pass. Both current- and test-kernel module builds pass.

The pending manual order in [Linux backend](linux-backend.md) is upload → ping →
region 5 → export → apply defaults. After successful preceding qualification:

```sh
printf 'apply\n' | sudo tee /sys/bus/platform/devices/NVDA200A:00/apply_config
sudo cat /sys/bus/platform/devices/NVDA200A:00/status
```

The latest uninstalled/unloaded artifact and source manifest are in
`results/npu1-bootstrap-config-apply-build/`; earlier build directories describe
earlier sources. No firmware file was installed and no device register written
by these tests. Later low-power configuration, IRQ routing/recovery, full
initialization and numerical inference remain incomplete.
