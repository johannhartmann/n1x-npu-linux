# Experimental N1X super-resolution demo

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

Use the audited P16/BIOS/npu2 kernel and acquire the pinned vendor libraries,
firmware and compiler inputs described in the repository documentation.
Follow the [setup guide](../docs/setup.md) to acquire pinned inputs and run
`venv/bin/python scripts/build_models.py --superres` in a fresh workspace.
Generated artifacts and vendor binaries remain in ignored `sources/` and
`results/`; they are not distributed in Git.

From the repository root, create the image-processing environment:

```sh
uv venv --python 3.12 sources/superres-venv
uv pip install --python sources/superres-venv/bin/python -r requirements-superres.txt
make module cudla
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
The launcher uses `pkexec` because the experimental driver requires RAWIO.
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
