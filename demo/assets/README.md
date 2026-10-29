# Published demo images

These are the complete sample and measured result, copied without re-encoding:

| File | Dimensions | SHA-256 |
| --- | --- | --- |
| [astronaut-input.png](astronaut-input.png) | 512x512 | `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5` |
| [astronaut-dla-3x.png](astronaut-dla-3x.png) | 1536x1536 | `7613dd25e1f01e1d495f256f32a48537465471c3a7a3fe5d38bb69843ab44ab5` |

The input is NASA's photograph of astronaut Eileen Collins, distributed as
[scikit-image's `astronaut` sample](https://scikit-image.org/docs/stable/api/skimage.data.html#skimage.data.astronaut),
documented there as public domain. The acquired file is pinned to
[scikit-image v0.19.3](https://github.com/scikit-image/scikit-image/blob/v0.19.3/skimage/data/astronaut.png).

The output is the actual N1X DLA run recorded in the
[physical hardware receipt](../../docs/evidence/npu-hardware-2026-10-28.json).
The four-convolution graph ran on DLA for 100 windows. CPU work handled color
conversion, chroma resizing, pixel rearrangement and the independent reference
check. There is no CPU fallback for the learned inference. This is a measured
example, not a general image-quality or sustained-performance claim.

The source model is ONNX Model Zoo's Apache-2.0 sub-pixel CNN, acquired
separately as described in [NOTICE](../../NOTICE). No weights, vendor loadable
or firmware are included with these images.

## Denoising example

| File | Dimensions | SHA-256 |
| --- | --- | --- |
| [astronaut-noisy-gray.png](astronaut-noisy-gray.png) | 512x512 | `7c5e85d4a542c74d6227a15972e58796e53b6f476c11e69bd46544588e93d4f3` |
| [astronaut-denoised-dla.png](astronaut-denoised-dla.png) | 512x512 | `0806ffc3bd55925c0456b22e5884c8626eb2a2c2ec19b9813d444b57d1e7b280` |

Both files are unchanged copies from the first complete physical DnCNN run.
The same NASA sample was converted to grayscale, perturbed with Gaussian noise
(sigma 25/255, NumPy seed 0), then quantized to FP16. The noisy PNG is a clipped,
8-bit rendering; the model consumed the retained numeric noisy input, including
values outside [0,1]. All 17 convolutions ran on NPU in three partitions and 64
overlapping windows. CPU work subtracted predicted noise and encoded the output.
The [vision receipt](../../docs/evidence/vision-hardware-2026-10-29.json) records
the original input hash, exact outputs, quality scores and independent reference
comparison. No pretrained weights or vendor instructions are distributed here.
