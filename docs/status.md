# Current support and qualification status

Updated 2026-10-29. This is the current summary; dated research documents retain
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

The [eight-job cuDLA receipt](evidence/cudla-hardware-2026-10-28.json) covers:

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
See [window receipt](evidence/superres-tile-2026-10-28.json). A prior compiler artifact
produced incorrect numbers; correcting packed-constant replay fixed the window.
No full image or sustained throughput is established by this result.

## Current physical lifecycle and image results

Module `1ED6855E6645BEEF8DA3534` passed two complete healthy
stop/unload/reload cycles in boot `dfa2cb44-dcfd-424a-a884-71a107503e69`.
At each of three starts, the firmware/bypass/ReLU sequence passed, followed by
eight independent-process cuDLA jobs and eight same-module changing-input jobs.
All **48 outputs** exactly matched independent references. The reuse groups
performed **15 REEXECs** and consumed one allocation per graph.

Both stops returned power reply `43 00` and independent state `07`; every
retained accel allocation was released. EXEC after stop returned ENODEV.
An open client prevented ordinary unload; closing it allowed normal removal
of the module, accel node and both endpoint bindings. Fresh load, firmware
startup and inference succeeded each time, without a reboot between cycles.

The complete image demo then passed on the final running instance:

| Input → output | DLA windows | Pipeline seconds | Maximum absolute error | RMSE |
| --- | --- | --- | --- | --- |
| 512x512 → 1536x1536 | 100 | 0.856 | 0.00158405 | 0.000162065 |
| 1024x1024 → 3072x3072 | 361 | 2.181 | 0.00153911 | 0.000158844 |

Each image added exactly one 4,550,656-byte retained allocation. Learned
inference ran on the physical DLA; whole-image FP32 ONNX execution independently
validated the outputs. Timings are one staged pipeline run per size, not a
sustained benchmark. The 1024 input was a bicubic enlargement of the pinned
512 NASA sample to validate the larger grid, not a native-resolution quality
benchmark. The existing `performance` profile was recorded and not changed.

Exact hashes, stop evidence, allocation counters and measured results are in
the [current hardware receipt](evidence/npu-hardware-2026-10-28.json).
The [first failed attempt](evidence/reload-attempt-2026-10-28.json) and
[offline review](evidence/lifecycle-review-2026-10-28.json) remain historical evidence.
Their host-mask assertion and state-byte issues are corrected in this tested
build. No failure recovery or long-term reliability claim follows from two
healthy reload cycles.

The fresh-checkout compiler pipeline reconstructs the three fixture models and
the super-resolution loadable with their expected hashes, without loading a
driver or accessing a device. See [setup](setup.md) and the
[offline reproduction receipt](evidence/offline-reproduction-2026-10-28.json).

## Denoising and classification

The [additional vision examples](../demo/vision.md) passed physical execution
and independent FP32 ONNX reference checks on the same audited module/kernel:

| Example | Physical result |
| --- | --- |
| DnCNN-25 denoising | Complete 512x512 grayscale restoration, 64 windows / 192 NPU submissions per image; PSNR 20.73 -> 31.83 dB, SSIM 0.328 -> 0.847 with reproducible synthetic Gaussian noise |
| ResNet-18 classification | 224x224 center crop; all 20 convolutions on NPU in 27 submissions; dog example top-1 Samoyed, 88.4% model score, matching the CPU reference |
| Reuse across images | Two-image batches passed for each application; second images added zero retained allocations |

Across the six successful single-image/batch cases, 657 application submissions
passed. Denoising max absolute error was 0.00108639, RMSE 0.000175177. ResNet
logit max absolute error was <= 0.014134, RMSE <= 0.003637, with matching top-1
for both inputs. ResNet's ambiguous astronaut image is a numerical check, not
a claim of correct semantic classification.

Denoising uses three partitions and 25,817,088 retained bytes; classification
uses twelve and 65,777,664 bytes. Several images can share loaded partitions in
one invocation. Separate invocations allocate again and may require the healthy
manual stop/start sequence to release backing. The measured batch lifecycle
passed three healthy stops, all retained counters returned to zero, and fresh
startup succeeded. The device was left running without application allocations.
No new kernel faults were found and the selected power profile was unchanged.

The classifier uses CPU preprocessing, im2col layout gathering, pooling,
residual operations and its fully connected head. The denoiser uses CPU tiling,
grayscale conversion and noise subtraction. Neither is a full NPU-only pipeline
or evidence of a speed/energy advantage. Exact results, timings and their scope
are in the [vision receipt](evidence/vision-hardware-2026-10-29.json).

## Remaining limits

Uncertain DMA/firmware errors remain fail-closed and require reboot. Automatic
recovery, suspend/resume, asynchronous scheduling, CUDA Graphs, zero-copy,
arbitrary TensorRT engines and general ONNX compilation are unsupported.
The two successful healthy reload cycles do not validate these paths.

## Announcement scope

The verified scope is experimental Linux N1X/DLA support on this P16, with
cuDLA inference, bounded healthy shutdown/reload qualification, complete
super-resolution and denoising images, and ResNet-18 classification. Vendor inputs and generated loadables are acquired
locally. The [setup](setup.md) and [demo](../demo/README.md) explain reproduction.
