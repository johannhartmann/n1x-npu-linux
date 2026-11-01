# Experimental N1X super-resolution demo

Also available: [DnCNN denoising and ResNet-18 classification](vision.md) · [Heterogeneous ONNX Execution Provider demo (`./demo-onnx`)](ort_demo.py).

This example applies the ONNX Model Zoo sub-pixel CNN to a complete image,
producing a 3x larger result through NVIDIA's original Linux cuDLA frontend
and this repository's N1X backend. Internal 64x64 windows use a five-pixel
halo; the output preserves the complete image and its aspect ratio.

[View the actual 1536x1536 DLA result](assets/astronaut-dla-3x.png) · [Image provenance](assets/README.md)

## Verification status

The complete physical P16 runs passed the independent whole-image ONNX
reference checks:

| Input → output | DLA windows | Pipeline seconds | Max absolute error | RMSE |
| --- | --- | --- | --- | --- |
| 512x512 → 1536x1536 | 100 | 0.856 | 0.00158405 | 0.000162065 |
| 1024x1024 → 3072x3072 | 361 | 2.181 | 0.00153911 | 0.000158844 |

Each image used exactly one retained 4,550,656-byte driver allocation, reused
for all windows. Two healthy unload/reload cycles and changing-input fixture
jobs passed first. See the [hardware receipt](../docs/evidence/npu-hardware-2026-10-28.json).
These are one-run staged pipeline timings, not sustained performance figures.
The 1024 input was enlarged with bicubic interpolation from the pinned NASA
512 sample to validate a larger inference grid; it is not a native-resolution
image quality benchmark. Learned inference in both runs executed on DLA.

The demo refuses to run on the older driver without completed-task I/O reuse.
See [controlled shutdown/reload](../docs/shutdown-reload.md) for the initial
reboot and qualification required to replace the old pinned module.

## Local prerequisites and execution

Use the qualified P16/BIOS NPU kernel and acquire the pinned vendor libraries,
firmware and compiler inputs described in the repository documentation.
Follow the [setup guide](../docs/setup.md) to acquire pinned inputs and run
`venv/bin/python scripts/build_models.py --superres` in a fresh workspace.
Generated artifacts and vendor binaries remain in ignored `sources/` and
`results/`; they are not distributed in Git.

From the repository root, create the image-processing environment:

```sh
uv venv --python 3.12 sources/superres-venv
uv pip install --python sources/superres-venv/bin/python --require-hashes \
  --only-binary :all: -r requirements-superres.lock
make module cudla
pkexec /usr/bin/python3 "$PWD/scripts/install_cudla_broker.py" --models superres
```

The required source model is `sources/demo-research/super-resolution-10.onnx`,
SHA-256 `85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e`.
The compiled artifact is `results/native-compile-t254-superres/superres.nvda`,
SHA-256 `c1ffde5d1eb9c7486a08ecc1470a1b633a11c7a71411f8fb36064d16dbcfc1c0`.
See the [model research](../docs/research/github-demo-research.md) for the model source
and license. The offline builder orchestrates `scripts/superres_graph.py`,
`scripts/compile_superres_backend.py` and `probe_native_graph.py` and checks
the final artifact hash. It performs no NPU submission.

After successful manual firmware startup and driver qualification:

```sh
./demo-superres /absolute/path/to/input.png --output-dir results/my-superres-run
```

The current platform power profile is recorded and left unchanged.
The output directory must be new. Input dimensions must both be at least 64.
The launcher itself stays unprivileged. It authenticates only the installed,
root-owned broker, which holds RAWIO and accepts packed tensor bytes for the
pinned model; image decoding, CPU validation and file output never run as root.
The output includes `upscaled.png`, raw DLA output planes, input and bicubic
comparisons, numerical metrics and an HTML report. Learned inference has no
CPU fallback. CPU preprocessing, chroma interpolation and pixel rearrangement
surround the DLA graph; independent CPU inference validates its raw output.
Execution failures stop the run without retry.

Offline image/stitching checks, requiring the source model:

```sh
sources/superres-venv/bin/python scripts/test_superres_image.py
```

The independent ONNX CPU reference may warn that generic device discovery
cannot read `accel0/device/vendor` on this ACPI platform device. The reference
explicitly uses CPUExecutionProvider; physical learned inference uses cuDLA.
That discovery warning does not indicate a fallback from DLA inference.

## Heterogeneous ONNX Execution Provider demo (`./demo-onnx`)

The top-level [`./demo-onnx`](../demo-onnx) launcher ([`demo/ort_demo.py`](ort_demo.py)) compiles, partitions, caches, and executes ONNX Model Zoo graphs through [`N1xInferenceSession`](../runtime/n1x_ort.py) in `FP16` and auto-calibrated `INT8`, producing `result.json` and an interactive `report.html`:

<p><a href="assets/emotion-ferplus-gallery-dla.png"><img src="assets/emotion-ferplus-gallery-dla.png" width="760" alt="FER+ 6-expression gallery evaluated on the physical N1X DLA in FP16 and INT8"></a></p>

[View the published FER+ gallery artifact](assets/emotion-ferplus-gallery-dla.png) · [Asset provenance and hashes](assets/README.md#fer-facial-emotion-recognition-example-demo-onnx---model-emotion)

| Model (`--model`) | Stages (`DLA` / `Host`) | Precision | `.nvda` size | Initial `EXEC` | Retained `REEXEC_IO` | Accuracy vs. FP32 ONNX Reference |
| --- | --- | --- | --- | --- | --- | --- |
| `emotion` (`emotion-ferplus-8.onnx`) | `1 DLA` / `0 Host` (37 nodes) | `FP16` | `21.21 MB` | `4.07 ms` | `1.37 ms` | `max_err = 0.001340`, `RMSE = 0.000811`, `cos = 1.000000` (`6/6` gallery top-1) |
| `emotion` (`emotion-ferplus-8.onnx`) | `1 DLA` / `0 Host` (37 nodes) | `INT8` | `12.47 MB` | `2.11 ms` | `0.98 ms` | `cos = 0.999930`, `RMSE = 0.031666` (`6/6` gallery top-1) |
| `mnist` (`mnist-12.onnx`) | `1 DLA` / `0 Host` (7 nodes) | `FP16` / `INT8` | `2.98 MB` | `0.83 ms` | `0.33 ms` | `FP16 max_err = 0.005589`, `INT8 cos = 0.999853` (`top-1` match) |
| `superres` (`super-resolution-10.onnx`) | `1 DLA` / `1 Host` (`DepthToSpace`) | `FP16` | `3.19 MB` | `1.36 ms` | `1.04 ms` | `max_err = 0.001510`, `RMSE = 0.000244` (`[1, 1, 192, 192]`) |

See the [ONNX Execution Provider hardware receipt](../docs/evidence/n1x-ort-hardware-2026-11-01.json) and [design document](../docs/designs/n1x-onnx-execution-provider.md).

### Pinned ONNX source models and artifacts

`venv/bin/python scripts/prepare_inputs.py --fetch` downloads and verifies the three ONNX Model Zoo graphs into ignored `sources/demo-research/`:

| Model alias (`--model`) | Local source path | Source ONNX SHA-256 | Compiled `.nvda` stages |
| --- | --- | --- | --- |
| `emotion` | `sources/demo-research/emotion-ferplus-8.onnx` | `a2a2ba6a335a3b29c21acb6272f962bd3d47f84952aaffa03b60986e04efa61c` | `1 DLAStage` (`21,210,412` B `FP16` / `12,465,324` B `INT8`), cached in `~/.cache/n1x_ort` |
| `mnist` | `sources/demo-research/mnist-12.onnx` | `5c688690f8bacf667d4c2074af5ad0646ca328d7ab03eccf944a65b320171bdd` | `1 DLAStage` (`2,975,356` B `FP16` / `INT8`), cached in `~/.cache/n1x_ort` |
| `superres` | `sources/demo-research/super-resolution-10.onnx` | `85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e` | `1 DLAStage` (`3,193,548` B `FP16`) + `1 HostStage` (`DepthToSpace`) |

Example invocations (with `make runtime` built and `/dev/accel/accel0` started; add `--simulate` to run without NPU hardware):

```sh
./demo-onnx --model emotion --precision fp16 --output-dir results/emotion-ferplus-fp16
./demo-onnx --model emotion --precision int8 --output-dir results/emotion-ferplus-int8
./demo-onnx --model mnist --precision fp16 --output-dir results/mnist-fp16
./demo-onnx --model mnist --precision int8 --output-dir results/mnist-int8
./demo-onnx --model superres --precision fp16 --output-dir results/superres-ort-fp16
```
