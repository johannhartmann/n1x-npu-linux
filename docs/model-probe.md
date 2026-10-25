# A numerical probe for the Windows ARM DLA compiler

This is a reproducible source model and CPU oracle, **not a compiled DLA
loadable or a working Linux inference path**. It replaces neither firmware
qualification nor the driver work. The legacy built-in diagnostic is gated
away for the reconstructed P16 runtime selector; see [the diagnostic audit](diagnostic-contract.md).

## Compiler route verified on 2026-10-25

NVIDIA's [TensorRT 11.4 documentation](https://docs.nvidia.com/deeplearning/tensorrt/latest/)
introduces Windows ARM support for DLA-capable RTX Spark laptops. The
[platform breakdown](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/work-with-dla.html)
states that DLA is beta on Windows ARM and unsupported on Linux SBSA in this
release. This provides a new compiler investigation route; it does not add
Linux NPU support or establish compatibility with our pinned driver 617.42.

The [Windows ARM layer specification](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dla-windows-on-arm.html)
lists FP16 ReLU with CHW shape `[32,7,7]` as validated, requires fixed dimensions
and batch 1, and provides FP16 CHW16 input/output formats. GPU fallback is
unavailable. The probe uses precisely that small activation shape and format.
Keep the ONNX source: NVIDIA requires rebuilding beta engines for general
availability. Compiler acceptance and numerical correctness still need testing.

## Generate the source and independent reference

```sh
venv/bin/python -m pip install -r requirements-probe.txt
make generate-probe
```

The generator writes ignored `results/relu-probe/`:

- `relu-fp16.onnx`: one ReLU, shape `[1,32,7,7]`, FP16, ONNX opset 13, IR 8.
- `input-nchw-fp16.bin`, `expected-nchw-fp16.bin`: little-endian logical tensors.
- `input-chw16-fp16.bin`, `expected-chw16-fp16.bin`: packed tensors for compiler I/O.
- `manifest.json`: sizes, hashes, shape, packing and reference package versions.

Each raw tensor is 3136 bytes. Inputs cycle through exactly representable
binary fractions from -2 to 2, including zero. The generator checks ONNX
structure and compares ONNX ReferenceEvaluator output byte-for-byte with
independently computed NumPy ReLU. It also checks the CHW16 packing roundtrip.
No subnormal, NaN or negative-zero behavior is assumed by this oracle.
Execution is CPU-only; a passing generator proves no NPU behavior.

## Candidate compile command (not yet run)

On a compatible Windows ARM TensorRT 11.4 installation, NVIDIA documents the
[standalone loadable workflow](https://docs.nvidia.com/deeplearning/tensorrt/11.4.0/inference-library/dla-standalone-mode.html).
Its `trtexec` form adapted to this model is:

```powershell
trtexec.exe --onnx=relu-fp16.onnx --saveEngine=relu-dla.bin --useDLACore=0 --safe --inputIOFormats=fp16:chw16 --outputIOFormats=fp16:chw16 --skipInference
```

This is an unexecuted candidate command, not a verified build recipe for this
machine. Tensor types provide FP16 precision; the removed `--fp16` flag is
not used. Preserve compiler version, logs, resulting file hash and the source
manifest before interpreting a produced file. Parse and compare its network,
instruction and address structures before allowing any Linux submission.

## Package investigation state

The official [NVIDIA Python package index](https://pypi.nvidia.com/tensorrt-cu13-libs/)
lists `tensorrt_cu13_libs-11.4.0.106-py3-none-win_arm64.whl`, advertised SHA-256
`a3231fed4887bed48c1da0c2f7a1b88ca39a5021e80870ab8b86ab99ce9b7391`.
Its remote ZIP directory was inspected: it contains `nvinfer_11.dll`,
`nvonnxparser_11.dll`, `nvinfer_plugin_11.dll` and a builder-resource DLL. No
separate DLA compiler DLL is listed. The complete download and code inspection
are pending; the archive's advertised hash is not yet a locally verified hash.
Windows binaries are not installed into the Linux Python environment. No
compiler execution, generated DLA binary or physical inference is claimed.
