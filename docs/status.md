# Current support and qualification status

Updated 2026-10-28. This is the current summary; dated research documents retain
their earlier observations and failed experiments. Source-code implementation,
offline tests and physical execution are separate evidence levels.

## Tested hardware and software

- ASUS ProArt P16 H7607BA, BIOS `H7607BA.300`, NVIDIA RTX Spark / N1X.
- NPU ACPI `NVDA200A`, compatible ID `NVDA2042`, revision `0x34`, DLA HAL 1.
- SROOT ACPI `NVDA8600`, revision 1; its own noncoherent DMA requester.
- Kernel `7.2.8-23-omarchy-p16-asus-npu2` and matching headers.
- Original NVIDIA Linux cuDLA 13.4.92 frontend, CUDA staging, reconstructed backend.
- Pinned Windows ARM64 driver 617.42 and TensorRT 11.4 compiler inputs.

Exact hashes and acquisition are in [setup](setup.md) and the qualification receipts.
The driver rejects other board/BIOS/kernel combinations. There is no claim of
universal N1X support or official NVIDIA Linux DLA support for this laptop.

## Physical results

The [eight-job cuDLA receipt](cudla-hardware-2026-10-28.json) covers:

| Graph | Inputs | Result |
| --- | --- | --- |
| FP16 ReLU | 2 different tensors | Exact independent reference matches |
| FP16 1x1 Conv with channel bias | 3 different tensors | Exact independent reference matches |
| FP16 Conv → ReLU | 3 different tensors | Exact independent reference matches |

All eight CUDA inputs remained unchanged. Firmware startup, DMA ping,
configuration and bypass passed first. The tested module retained 22,953,984
bytes for those eight jobs. That receipt describes the older retention-only
driver, not physical validation of newer reuse or cleanup code.

The trained four-convolution super-resolution graph also produced a correct
physical 64x64 input window. Its nine output planes had maximum absolute error
`0.0009882450` and RMSE `0.0000607435` against the independent FP32 ONNX reference.
See [window receipt](superres-tile-2026-10-28.json). A prior compiler artifact
produced incorrect numbers; correcting packed-constant replay fixed the window.
No full image or sustained throughput is established by this result.

The post-reboot test with module `0FD7E6269B085ED9DAD6DB9` also passed the
startup ReLU probe and eight numerical cuDLA jobs under the unchanged `quiet`
platform profile (`power-saver` in power-profiles-daemon). Its first shutdown
failed at an overly strict host IRQ-mask readback assertion **before** HFRP
power-off. DMA and module references remained retained; no reload was attempted.
The assertion is corrected and offline-tested, with raw-mask diagnostics added,
but needs another reboot for hardware qualification. See the
[attempt receipt](reload-attempt-2026-10-28.json).

## Implemented, awaiting physical qualification

- Completed-task I/O reuse with one retained allocation per loaded model.
  Driver and backend tests cover repeated reuse, ownership and sticky errors.
- Checked immediate shutdown, power-state confirmation, DMA cleanup, balanced
  runtime/module references and ordinary module unload/reload.
- Full-image 3x super-resolution with static windows, halo cropping and exact
  borders. CPU whole-image/reference checks pass through 1024x1024 → 3072x3072.

The fresh-checkout compiler pipeline reconstructs the three fixture models and
the super-resolution loadable with their expected hashes, without loading a
driver or accessing a device. See [setup](setup.md) and the
[offline reproduction receipt](offline-reproduction-2026-10-28.json).

## Remaining post-reboot checks

1. Reboot the same audited `npu2` kernel to remove the instance with the failed
   terminal stop; the corrected build is ready locally.
2. Run [shutdown/reload qualification](shutdown-reload.md): healthy startup,
   numerical jobs, checked stop, zero retained allocations, open-client unload
   refusal, successful unload after close, fresh startup and numerical jobs.
3. Run the complete [512x512 demo](../demo/README.md), then 1024x1024; inspect
   the actual DLA output, numerical metrics, allocation counters and kernel logs.
4. Record measured outcomes before promoting these paths to physically verified.

Uncertain DMA/firmware errors remain fail-closed and require reboot. Automatic
recovery, suspend/resume, asynchronous scheduling, CUDA Graphs, zero-copy,
arbitrary TensorRT engines and general ONNX compilation are unsupported.

## Announcement scope

The supported announcement is an experimental, source-available Linux N1X/DLA
bring-up with verified cuDLA inference on one P16 and reproducible offline
compiler work. Full-image hardware demo and reload claims must wait for the
checks above. Vendor inputs and generated loadables are acquired locally.
