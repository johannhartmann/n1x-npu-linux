# A reproducible public N1X cuDLA demo

> Historical research record. This document preserves observations from earlier
> bring-up stages, including superseded status claims and commands for older builds.
> Use [current status](../status.md) and [setup](../setup.md) for the tested behavior
> and current procedure. Command paths below are relative to the repository root.

Research date: 2026-10-28. Recommendation only: neither model below has been
compiled for N1X or executed on this DLA. Existing physical qualification
covers the three small fixtures described in [the backend guide](../cudla-backend.md).

## Recommendation: single-image 3x super-resolution

Use the ONNX Model Zoo's compact sub-pixel CNN as the first application demo.
It produces an immediately understandable before/after image and its learned
portion uses the two operation families already exercised by this project:
convolution and ReLU. This still requires new compiler and hardware work.

Primary references:

- [Model and preprocessing](https://github.com/onnx/models/blob/main/validated/vision/super_resolution/sub_pixel_cnn_2016/README.md)
- [PyTorch architecture](https://github.com/pytorch/examples/blob/main/super_resolution/model.py)
- [NVIDIA Windows ARM layer specification](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-windows-on-arm.html)

The actual ONNX binary was downloaded and checked with `onnx.checker`, rather
than inferring its graph from the README:

| Property | Inspected artifact |
| --- | --- |
| File | `super-resolution-10.onnx` |
| Size | 240,078 bytes |
| SHA-256 | `85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e` |
| Learned graph | 4 Conv, 3 ReLU |
| Conv weights | `[64,1,5,5]`, `[64,64,3,3]`, `[32,64,3,3]`, `[9,32,3,3]` |
| Conv attributes | stride 1, dilation 1, group 1; padding 2 for 5x5, 1 for 3x3 |
| Final layout | 2 Constant, 2 Reshape, 1 Transpose implementing 3x pixel shuffle |
| Original image shape | Y luminance, 224x224 -> 672x672; batch dimension symbolic |
| Precision | FP32 source; target FP16 conversion and accuracy check required |

The README carries Apache-2.0 SPDX metadata; retain the upstream license and
notices when packaging model/source artifacts. It identifies BSD300 as the
training dataset. A user-supplied PNG avoids bundling third-party photographs.
The 240-KB weight file is not the runtime memory requirement.

### Proposed user experience

One command accepts a PNG and creates a self-contained report: source crop,
bicubic enlargement, DLA enlargement, reference comparison and timings. A
proposed command such as `./demo-superres image.png` is a design target, not
an existing command in this repository.

The user-facing target is at least **512x512 -> 1536x1536**, with
**1024x1024 -> 3072x3072** as the next acceptance size. Preserve the complete
image and its aspect ratio; do not silently replace it with a tiny crop.
Small internal tiles are an implementation detail, not the advertised image
size. Include enlarged detail comparisons so the result is easy to inspect.

For the first implementation:

1. Fix batch to 1 and compile a bounded tile shape selected from measured
   materialized memory use. Process the full image using overlapping tiles.
   These are new graph variants: the original 224/672 reshape constants must
   not survive a dimension change. Validate each variant independently.
2. Extract YCbCr luminance on the CPU. Run all four learned convolutions and
   their ReLUs on DLA, in one compiled task if the compiler permits it.
3. Run the final pixel shuffle on the CPU as an exact layout permutation,
   resize chroma with bicubic interpolation, and combine the RGB output.
   The report should identify these CPU preprocessing/postprocessing stages.
4. Compare with an independent ONNX reference using numerical metrics and
   a measured FP16 tolerance. Do not demand the bitwise FP32 equality used
   for our specially constructed arithmetic fixtures.
5. Measure compilation, CUDA staging, submission and complete application
   latency separately. Make no speedup claim before measuring it.

Use one image per invocation. Each tile must fit the current 32-MiB per-job
limit. A multi-tile image must also respect the 64-MiB retained limit; tiling
alone does not remove that constraint. Prove safe task retirement and reuse
of bounded DMA backing before relying on repeated tile execution. Do not
increase the limits blindly or require a reboot between tiles.

The inspected four stride-1 convolutions have receptive field 11: kernel 5
followed by three kernel-3 layers. An interior tile therefore needs five
low-resolution pixels of context on each side; discard the corresponding
15-pixel output halo after 3x pixel shuffle. Global image edges must preserve
the original network's per-layer zero-padding semantics. Compare tiled and
whole-image reference execution, including seams, corners, non-square images
and dimensions that do not divide the tile size. Naively padding the source
outside the image can change intermediate activations and is not automatically
equivalent to the original graph.

### Missing implementation before others can reproduce it

- Compile spatial 3x3/5x5 convolutions, four-layer topology, new channels,
  dimensions and trained constants. Our current bridge is pinned to the
  1x1 32-channel fixture and replays checked Windows backend results; it is
  not an arbitrary ONNX-to-N1X compiler.
- Qualify compiler acceptance, padding, channel packing, intermediate memory
  and numerical output on physical N1X. NVIDIA's tested configuration table
  is not proof of support for this model's exact tuples.
- Implement and qualify safe DMA buffer reuse, then tiled inference with halo
  cropping and exact image-edge handling. The user-facing size requirement
  must not be weakened to fit the current retention policy.
- Provide a pinned acquisition/build path for the Windows driver, compiler,
  original Linux cuDLA frontend and board prerequisites. Files in ignored
  local `sources/` and `results/` are not available to GitHub users.
- Publish the model source, conversion code, reference test, licenses and
  supported-machine check alongside the final example. The repository lacks
  a top-level license file today; existing source SPDX notices should be
  reflected in the distribution's license documentation.

## Alternative: handwritten digit recognition

[ONNX Model Zoo MNIST](https://github.com/onnx/models/blob/main/validated/vision/classification/mnist/README.md)
is smaller and carries an explicit MIT model license. A PNG -> predicted
digit + ten scores makes a good correctness-oriented demo.

The actual `mnist-12.onnx` is 26,143 bytes, SHA-256
`5c688690f8bacf667d4c2074af5ad0646ca328d7ab03eccf944a65b320171bdd`.
Inspection finds 2 Conv, 3 Add, 2 ReLU, 2 MaxPool, 2 Reshape and 1 MatMul.
It expects `[1,1,28,28]` FP32 input and returns ten scores. It therefore needs
more new operator lowering than super-resolution, despite its smaller file.
Some Add nodes may fold into convolution bias, but that must be validated.

The [official PyTorch tutorial](https://github.com/pytorch/tutorials/blob/main/beginner_source/nn_tutorial.py)
offers a different route: a tiny three-convolution classifier with average
pooling, under the tutorial repository's BSD-3-Clause license. Retraining a
convolution-only variant could avoid MatMul, at the cost of maintaining a
separate trained model and its accuracy/reproducibility checks.

## Why not start with NVIDIA's YOLO demo?

[NVIDIA-AI-IOT/cuDLA-samples](https://github.com/NVIDIA-AI-IOT/cuDLA-samples)
is a valuable CUDA -> DLA -> CUDA pipeline reference, but targets Orin and
YOLOv5. It is not evidence that its loadables run on N1X. Our staged, blocking
backend and retained-allocation limits do not yet support an equivalent
continuous camera demo. Similarly, the layerwise-statistics examples depend
on profiling exports that this backend currently declines.

The downloaded ONNX files are kept only in ignored `sources/demo-research/`.
No hardware submissions, driver changes or demo installation were performed
during this research.
