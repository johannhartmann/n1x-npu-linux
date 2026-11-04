# Published Demo Assets

[`scripts/check_repo.py`](../../scripts/check_repo.py) verifies the SHA-256 digests of these committed demo images against the hardware qualification receipts in [`docs/evidence/`](../../docs/evidence/README.md).

## Super-Resolution (`./demo-superres`)

| File | Dimensions | SHA-256 | Receipt |
| --- | --- | --- | --- |
| [astronaut-input.png](astronaut-input.png) | 512×512 | `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5` | [`npu-hardware-2026-10-28.json`](../../docs/evidence/npu-hardware-2026-10-28.json) |
| [astronaut-dla-3x.png](astronaut-dla-3x.png) | 1536×1536 | `7613dd25e1f01e1d495f256f32a48537465471c3a7a3fe5d38bb69843ab44ab5` | [`npu-hardware-2026-10-28.json`](../../docs/evidence/npu-hardware-2026-10-28.json) |

Input: NASA photograph of astronaut Eileen Collins ([scikit-image v0.19.3 `astronaut.png`](https://github.com/scikit-image/scikit-image/blob/v0.19.3/skimage/data/astronaut.png), public domain).

## Denoising (`./demo-denoise`)

| File | Dimensions | SHA-256 | Receipt |
| --- | --- | --- | --- |
| [astronaut-noisy-gray.png](astronaut-noisy-gray.png) | 512×512 | `7c5e85d4a542c74d6227a15972e58796e53b6f476c11e69bd46544588e93d4f3` | [`vision-hardware-2026-10-29.json`](../../docs/evidence/vision-hardware-2026-10-29.json) |
| [astronaut-denoised-dla.png](astronaut-denoised-dla.png) | 512×512 | `0806ffc3bd55925c0456b22e5884c8626eb2a2c2ec19b9813d444b57d1e7b280` | [`vision-hardware-2026-10-29.json`](../../docs/evidence/vision-hardware-2026-10-29.json) |

## FER+ facial emotion recognition example (`./demo-onnx --model emotion`)

| File | Dimensions | SHA-256 | Receipt |
| --- | --- | --- | --- |
| [emotion-ferplus-gallery-dla.png](emotion-ferplus-gallery-dla.png) | 1080×520 | `933733179625438bcec69679960cbf8309ae223007540af39d84c19831740dc0` | [`n1x-ort-hardware-2026-11-01.json`](../../docs/evidence/n1x-ort-hardware-2026-11-01.json) |

Rendered from the physical N1X DLA outputs of [`./demo-onnx --model emotion`](../ort_demo.py) in `FP16` and auto-calibrated `INT8`. See [NOTICE](../../NOTICE) for upstream model licenses.
