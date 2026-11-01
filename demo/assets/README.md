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

## FER+ facial emotion recognition example (`./demo-onnx --model emotion`)

| File | Dimensions | SHA-256 |
| --- | --- | --- |
| [emotion-ferplus-gallery-dla.png](emotion-ferplus-gallery-dla.png) | 1080x520 | `933733179625438bcec69679960cbf8309ae223007540af39d84c19831740dc0` |

This gallery composite is rendered directly from the physical N1X DLA outputs
recorded by [`./demo-onnx --model emotion`](../ort_demo.py) in `FP16` and
auto-calibrated `INT8` (`results/emotion-ferplus-fp16/result.json` and
`results/emotion-ferplus-int8/result.json`), matching the
[ONNX Execution Provider hardware receipt](../../docs/evidence/n1x-ort-hardware-2026-11-01.json).
All 37 ONNX nodes (8 `Conv`, 8 `MaxPool`, 3 `Dense`/`MatMul` layers, plus constant
`Sub`/`Div` input normalization) are lowered into a single `t254` `.nvda` loadable
(`1 DLAStage`, `0 HostStages`, `21.21 MB` in `FP16` / `12.47 MB` in `INT8`) and
executed across the 6-preset 64x64 facial expression gallery with retained
`REEXEC_IO` reuse (`1.37 ms` in `FP16`, `0.98 ms` in `INT8`), matching the
independent FP32 ONNX reference top-1 prediction on all `6/6` expressions.
The source model is ONNX Model Zoo's MIT-licensed `emotion-ferplus-8.onnx`,
acquired separately via `scripts/prepare_inputs.py --fetch` as described in
[NOTICE](../../NOTICE). No model weights or `.nvda` loadables are included with
this image.
