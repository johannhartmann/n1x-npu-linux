# Qualification Receipts

Tracked JSON receipts record artifact SHA-256 digests and physical/offline verification outputs. [`scripts/check_repo.py`](../../scripts/check_repo.py) verifies that published demo assets in [`demo/assets/`](../../demo/assets/README.md) match these receipts.

| Receipt | Scope |
| --- | --- |
| [`n1x-ort-hardware-2026-11-01.json`](n1x-ort-hardware-2026-11-01.json) | `N1xInferenceSession` physical qualification on `mnist-12.onnx`, `emotion-ferplus-8.onnx`, `super-resolution-10.onnx`, and 3-stage `DLA -> Host -> DLA` (`FP16` & `INT8`, `REEXEC_IO`) |
| [`npu-hardware-2026-10-28.json`](npu-hardware-2026-10-28.json) | 48 cuDLA fixture jobs, 15 `REEXEC`s, 2 stop/unload/reload cycles, and 512×512 / 1024×1024 super-resolution runs |
| [`vision-hardware-2026-10-29.json`](vision-hardware-2026-10-29.json) | 6 DnCNN denoising and ResNet-18 classification runs (657 NPU submissions) |
| [`vision-reproduction-2026-10-29.json`](vision-reproduction-2026-10-29.json) | Offline reproduction of all 15 DnCNN and ResNet-18 partition `.nvda` loadables |
| [`offline-reproduction-2026-10-28.json`](offline-reproduction-2026-10-28.json) | Offline reproduction of the `relu`, `conv1x1`, `conv1x1_relu`, and `superres` `.nvda` loadables |
| [`cudla-hardware-2026-10-28.json`](cudla-hardware-2026-10-28.json) | Initial 8-job physical cuDLA qualification |
| [`superres-tile-2026-10-28.json`](superres-tile-2026-10-28.json) | Single 64×64 super-resolution tile verification |
| [`reload-attempt-2026-10-28.json`](reload-attempt-2026-10-28.json), [`lifecycle-review-2026-10-28.json`](lifecycle-review-2026-10-28.json) | Earlier shutdown mask-readback diagnosis and transport verification |
