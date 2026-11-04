# Support and Qualification Status

## Qualified Platform

- **System**: ASUS ProArt P16 H7607BA, BIOS `H7607BA.300`, NVIDIA RTX Spark / N1X (`t254`).
- **ACPI Endpoints**: `NVDA200A` (`_HRV=0x34`, DLA HAL 1, SMMUv3 stream `0x2b`) and `NVDA8600` (`_HRV=1`, noncoherent `SROOT` DMA requester, SMMUv3 stream `0x01`).
- **Kernel**: `7.2.8-23-rtx-spark-npu` (includes the [`NVDA8600` noncoherent DMA quirk](../patches/linux-7.2.8-p16-sroot-noncoherent-dma.patch); see [P16 Linux setup](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux)).
- **Vendor Inputs**: Windows ARM64 driver `617.42` (`nvlddmkm.sys`, `nvdla_runtime.dll`), TensorRT `11.4` (`libnvdla_compiler.so`, `nvdla_compiler.dll`), and Linux cuDLA `13.4.92` (`libcudla.so.1`).

By default (`strict_platform=0`, `autostart=1`), `n1x_npu_bootstrap.ko` dynamically discovers the ACPI endpoints, `_CRS` MMIO apertures, IORT SMMUv3 mappings, and live VBIOS policy carveout (`0x1182bc80`), brings up firmware in C on probe, and exposes `/dev/accel/accel0`. Passing `strict_platform=1` enforces exact DMI board/BIOS matching.

## Hardware Qualification Summary

| Workload | Configuration | Measured Result | Receipt |
| --- | --- | --- | --- |
| cuDLA fixtures & module reload | `FP16` ReLU, `1x1` Conv, `Conv -> ReLU` (48 jobs, 15 `REEXEC`s, 2 stop/unload/reload cycles) | 48/48 exact reference matches; `0` retained bytes after stop | [`npu-hardware-2026-10-28.json`](evidence/npu-hardware-2026-10-28.json), [`cudla-hardware-2026-10-28.json`](evidence/cudla-hardware-2026-10-28.json), [`superres-tile-2026-10-28.json`](evidence/superres-tile-2026-10-28.json) |
| 3× super-resolution (`./demo-superres`) | 512×512 → 1536×1536 (100 tiles) and 1024×1024 → 3072×3072 (361 tiles) | `0.856 s` (`RMSE 0.000162`) and `2.181 s` (`RMSE 0.000159`) | [`npu-hardware-2026-10-28.json`](evidence/npu-hardware-2026-10-28.json), [`offline-reproduction-2026-10-28.json`](evidence/offline-reproduction-2026-10-28.json) |
| DnCNN-25 denoising (`./demo-denoise`) | 512×512 grayscale, 3 partitions, 64 windows (192 submissions/image) | PSNR `20.73 -> 31.83 dB`, SSIM `0.328 -> 0.847`, `RMSE 0.000175` | [`vision-hardware-2026-10-29.json`](evidence/vision-hardware-2026-10-29.json), [`vision-reproduction-2026-10-29.json`](evidence/vision-reproduction-2026-10-29.json) |
| ResNet-18 classification (`./demo-classify`) | 224×224 crop, 20 convolutions in 12 partitions (27 submissions/image) | Top-1 Samoyed (`88.4%`), `RMSE <= 0.003637` vs. FP32 reference | [`vision-hardware-2026-10-29.json`](evidence/vision-hardware-2026-10-29.json) |
| ONNX EP: `mnist-12.onnx` | 1 `DLAStage` (`2.98 MB` `.nvda`), `FP16` & auto-calibrated `INT8` | `0.833 ms` `EXEC`, `0.332 ms` `REEXEC_IO`; `FP16 RMSE 0.002614`, `INT8 cos 0.999853` | [`n1x-ort-hardware-2026-11-01.json`](evidence/n1x-ort-hardware-2026-11-01.json) |
| ONNX EP: `emotion-ferplus-8.onnx` | 1 `DLAStage` (37 nodes, `21.21 MB` `FP16` / `12.47 MB` `INT8`) | `1.37 ms` `FP16` / `0.98 ms` `INT8` `REEXEC_IO`; `6/6` gallery top-1 matches | [`n1x-ort-hardware-2026-11-01.json`](evidence/n1x-ort-hardware-2026-11-01.json) |
| ONNX EP: `super-resolution-10.onnx` | `1 DLAStage` + `1 HostStage` (`DepthToSpace`) | `1.357 ms` `EXEC`, `1.037 ms` `REEXEC_IO`; `RMSE 0.000244` | [`n1x-ort-hardware-2026-11-01.json`](evidence/n1x-ort-hardware-2026-11-01.json) |
| Dynamic frequency scaling | `/sys/bus/platform/devices/NVDA200A:00/npu_freq_khz` (`892.8 MHz` → `1500 MHz`) | 64-tile super-resolution reduced from `224.2 ms` to `97.1 ms` (`2.31x`) | [Architecture](architecture.md#2-vbios-policy-hfrp-bring-up--frequency-scaling) |

## Current Boundaries

- Non-DLA ONNX operators (`DepthToSpace`, `ReduceMean`, `Resize`, `Softmax`, dynamic `Reshape`/`Transpose`) run on the host CPU via [`N1xInferenceSession`](../runtime/n1x_ort.py)'s graph partitioner.
- Completed jobs owned by an open `/dev/accel/accel0` file descriptor are freed automatically in `postclose()` (and `vm_close()` when mapped via `mmap()`). Uncertain DMA/firmware errors or a failed hardware stop (`accel->stop_failed`) remain fail-closed and retain DMA allocations until confirmed hardware shutdown or reboot.
- Live hardware reset after a hung firmware channel, multi-queue asynchronous scheduling, CUDA Graphs, and `DMA-BUF` / `NvSci` import are not implemented.
