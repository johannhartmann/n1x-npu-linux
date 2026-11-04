#!/usr/bin/env python3
"""Offline verification of native libn1x_compiler.so, DAG lowering, and INT8 support."""

import base64
import ctypes as c
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import cudla_python
from loadable import Model
from n1x_compiler import (
    build_reference_dag,
    compile_dag,
    compile_onnx,
)
from n1x_dla import (
    dequantize_int8,
    pack_chw16_int8,
    pack_chw32_int8,
    pack_tensor_for_desc,
    quantize_int8,
    unpack_chw16_int8,
    unpack_chw32_int8,
    unpack_tensor_from_desc,
)

REFERENCE_SHA256 = {
    "relu": "561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942",
    "conv1x1": "717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b",
    "conv1x1_relu": "fe57704f93edbafe9e374e14675cfc34cc5a44858497c6126dec56a60a837536",
    "superres": "c1ffde5d1eb9c7486a08ecc1470a1b633a11c7a71411f8fb36064d16dbcfc1c0",
}


def _b64(arr: np.ndarray, dtype: str) -> str:
    return base64.b64encode(np.asarray(arr, dtype=dtype).tobytes()).decode("ascii")


def _check_cpp_materializer(nvda_bytes: bytes) -> int:
    lib_path = ROOT / "runtime/cudla/libnvcudla.so"
    if not lib_path.exists():
        return len(Model(nvda_bytes).image)
    lib = c.CDLL(str(lib_path))
    query = lib.n1x_cudla_materialized_image_size
    query.argtypes = (c.c_void_p, c.c_size_t, c.POINTER(c.c_uint64))
    query.restype = c.c_int
    buf = c.create_string_buffer(nvda_bytes)
    out = c.c_uint64()
    rc = query(buf, len(nvda_bytes), c.byref(out))
    assert rc == 0, f"C++ cudla_model rejected loadable: rc={rc}"
    return int(out.value)


def test_reference_reproduction() -> dict:
    digests = {}
    for name, expected_sha in REFERENCE_SHA256.items():
        dag = build_reference_dag(name)
        nvda = compile_dag(dag)
        digest = hashlib.sha256(nvda).hexdigest()
        assert digest == expected_sha, f"{name} digest mismatch: {digest} != {expected_sha}"
        model = Model(nvda)
        assert _check_cpp_materializer(nvda) == len(model.image)
        digests[name] = digest

    onnx_path = ROOT / "sources/demo-research/super-resolution-10.onnx"
    if onnx_path.exists():
        nvda_onnx = compile_onnx(
            onnx_path,
            input_shapes={"input": [1, 1, 64, 64]},
            include_pixel_shuffle=False,
        )
        assert hashlib.sha256(nvda_onnx).hexdigest() == REFERENCE_SHA256["superres"]
    return digests


def test_cli_binary() -> None:
    cli_path = ROOT / "runtime/n1x-compile"
    assert cli_path.exists(), "runtime/n1x-compile binary is missing"
    dag = build_reference_dag("conv1x1_relu")
    with tempfile.TemporaryDirectory(prefix="n1x-compile-cli-") as tmp:
        tmp_dir = Path(tmp)
        json_file = tmp_dir / "graph.json"
        out_file = tmp_dir / "out.nvda"
        json_file.write_text(json.dumps(dag), encoding="utf-8")
        subprocess.run(
            [str(cli_path), str(json_file), "-o", str(out_file)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        digest = hashlib.sha256(out_file.read_bytes()).hexdigest()
        assert digest == REFERENCE_SHA256["conv1x1_relu"]


def test_residual_dag_and_multi_io() -> dict:
    c_ch = 16
    rng = np.random.default_rng(42)
    w1 = rng.uniform(-0.2, 0.2, size=(c_ch, c_ch, 3, 3))
    b1 = rng.uniform(-0.05, 0.05, size=(c_ch,))
    w2 = rng.uniform(-0.2, 0.2, size=(c_ch, c_ch, 3, 3))
    b2 = rng.uniform(-0.05, 0.05, size=(c_ch,))
    sc = np.ones((c_ch,))
    sh = np.zeros((c_ch,))
    bn_m = np.zeros((c_ch,))
    bn_v = np.ones((c_ch,))
    fc_w = rng.uniform(-0.1, 0.1, size=(8, c_ch, 1, 1))
    fc_b = np.zeros((8,))

    residual_dag = {
        "target": "t254",
        "compute_precision": "fp16",
        "inputs": [{"name": "input", "shape": [1, c_ch, 8, 8], "dtype": "fp16", "data_format": "chw16"}],
        "outputs": [{"name": "output", "tensor_name": "output", "dtype": "fp16", "data_format": "chw16"}],
        "layers": [
            {
                "op": "Convolution", "name": "conv1", "input": "input", "output": "c1",
                "out_channels": c_ch, "kernel": [3, 3], "stride": [1, 1],
                "padding": [1, 1, 1, 1], "dilation": [1, 1], "groups": 1,
                "bias_mode": "channel",
                "weights_b64": _b64(w1, "<f2"), "weights_dtype": "fp16",
                "bias_b64": _b64(b1, "<f2"), "bias_dtype": "fp16",
            },
            {
                "op": "Scale", "name": "scale1", "input": "c1", "output": "s1",
                "mode": "channel",
                "scale_b64": _b64(sc, "<f2"), "scale_dtype": "fp16",
                "shift_b64": _b64(sh, "<f2"), "shift_dtype": "fp16",
            },
            {"op": "Activation", "name": "relu1", "input": "s1", "output": "r1", "mode": "relu"},
            {
                "op": "Convolution", "name": "conv2", "input": "r1", "output": "c2",
                "out_channels": c_ch, "kernel": [3, 3], "stride": [1, 1],
                "padding": [1, 1, 1, 1], "dilation": [1, 1], "groups": 1,
                "bias_mode": "channel",
                "weights_b64": _b64(w2, "<f2"), "weights_dtype": "fp16",
                "bias_b64": _b64(b2, "<f2"), "bias_dtype": "fp16",
            },
            {
                "op": "BatchNorm", "name": "bn2", "input": "c2", "output": "b2_out",
                "mode": "channel",
                "mean_b64": _b64(bn_m, "<f2"), "mean_dtype": "fp16",
                "variance_b64": _b64(bn_v, "<f2"), "variance_dtype": "fp16",
                "eps": 1e-5,
            },
            {"op": "ElementWise", "name": "res_add", "inputs": ["input", "b2_out"], "output": "sum1", "mode": "sum"},
            {"op": "Activation", "name": "relu2", "input": "sum1", "output": "r2", "mode": "relu"},
            {
                "op": "Pooling", "name": "avgpool", "input": "r2", "output": "p1",
                "mode": "average", "window": [8, 8], "stride": [8, 8], "padding": [0, 0, 0, 0],
            },
            {
                "op": "FullyConnected", "name": "fc", "input": "p1", "output": "output",
                "out_channels": 8, "bias_mode": "channel",
                "weights_b64": _b64(fc_w, "<f2"), "weights_dtype": "fp16",
                "bias_b64": _b64(fc_b, "<f2"), "bias_dtype": "fp16",
            },
        ],
    }
    res_nvda = compile_dag(residual_dag)
    res_model = Model(res_nvda)
    assert _check_cpp_materializer(res_nvda) == len(res_model.image)

    multi_io_dag = {
        "target": "t254",
        "compute_precision": "fp16",
        "inputs": [
            {"name": "in_a", "shape": [1, 16, 4, 4], "dtype": "fp16", "data_format": "chw16"},
            {"name": "in_b", "shape": [1, 16, 4, 4], "dtype": "fp16", "data_format": "chw16"},
        ],
        "outputs": [
            {"name": "out_sum", "tensor_name": "out_sum", "dtype": "fp16", "data_format": "chw16"},
            {"name": "out_relu", "tensor_name": "out_relu", "dtype": "fp16", "data_format": "chw16"},
        ],
        "layers": [
            {"op": "ElementWise", "name": "ew_sum", "inputs": ["in_a", "in_b"], "output": "out_sum", "mode": "sum"},
            {"op": "Activation", "name": "act_relu", "input": "out_sum", "output": "out_relu", "mode": "relu"},
        ],
    }
    multi_nvda = compile_dag(multi_io_dag)
    multi_model = Model(multi_nvda)
    assert len(multi_model.inputs) == 2 and len(multi_model.outputs) == 2
    assert _check_cpp_materializer(multi_nvda) == len(multi_model.image)
    desc_info = cudla_python.describe_model(multi_model)
    assert len(desc_info["inputs"]) == 2 and len(desc_info["outputs"]) == 2
    return {"residual_bytes": len(res_nvda), "multi_io_bytes": len(multi_nvda)}


def test_int8_compilation_and_packing() -> dict:
    # 1. Implicit INT8 calibration
    dag_int8 = build_reference_dag("conv1x1_relu")
    dag_int8["compute_precision"] = "int8"
    for t in dag_int8["inputs"] + dag_int8["outputs"]:
        t["dtype"] = "int8"
        t["data_format"] = "chw32"
        t["dynamic_range"] = [-2.0, 2.0]
    for l in dag_int8["layers"]:
        l["dynamic_range"] = [-2.0, 2.0]
    nvda_int8 = compile_dag(dag_int8)
    model_int8 = Model(nvda_int8)
    assert model_int8.inputs[0].format_codes[1] == 4
    assert model_int8.outputs[0].format_codes[1] == 4
    assert _check_cpp_materializer(nvda_int8) == len(model_int8.image)

    # 2. Explicit Q/DQ compilation
    in_scale = np.array([0.05], dtype="<f4")
    in_zp = np.array([0], dtype="<i1")
    w = np.ones((16, 16, 3, 3), dtype="<f4") * 0.1
    b = np.zeros((16,), dtype="<f4")
    qdq_dag = {
        "target": "t254",
        "compute_precision": "fp16",
        "explicit_qdq": True,
        "inputs": [{"name": "input", "shape": [1, 16, 8, 8], "dtype": "fp16", "data_format": "chw16"}],
        "outputs": [{"name": "output", "tensor_name": "output", "dtype": "fp16", "data_format": "chw16"}],
        "layers": [
            {
                "op": "Quantize", "name": "q_in", "input": "input", "output": "x_q",
                "axis": 1,
                "scales_b64": _b64(in_scale, "<f4"), "scales_dtype": "fp32",
                "zero_points_b64": _b64(in_zp, "<i1"), "zero_points_dtype": "int8",
            },
            {
                "op": "Dequantize", "name": "dq_in", "input": "x_q", "output": "x_dq",
                "axis": 1,
                "scales_b64": _b64(in_scale, "<f4"), "scales_dtype": "fp32",
                "zero_points_b64": _b64(in_zp, "<i1"), "zero_points_dtype": "int8",
            },
            {
                "op": "Convolution", "name": "conv", "input": "x_dq", "output": "output",
                "out_channels": 16, "kernel": [3, 3], "stride": [1, 1],
                "padding": [1, 1, 1, 1], "dilation": [1, 1], "groups": 1,
                "bias_mode": "channel",
                "weights_b64": _b64(w, "<f4"), "weights_dtype": "fp32",
                "bias_b64": _b64(b, "<f4"), "bias_dtype": "fp32",
            },
        ],
    }
    nvda_qdq = compile_dag(qdq_dag)
    model_qdq = Model(nvda_qdq)
    assert _check_cpp_materializer(nvda_qdq) == len(model_qdq.image)

    # 3. INT8 CHW32 & CHW16 packing/unpacking round-trip
    rng = np.random.default_rng(7)
    arr_int8 = rng.integers(-128, 128, size=(1, 32, 7, 7), dtype=np.int8)
    packed_32 = pack_chw32_int8(arr_int8, stride_w=256)
    unpacked_32 = unpack_chw32_int8(packed_32, (1, 32, 7, 7), stride_w=256)
    assert np.array_equal(arr_int8, unpacked_32)

    packed_16 = pack_chw16_int8(arr_int8, stride_w=256)
    unpacked_16 = unpack_chw16_int8(packed_16, (1, 32, 7, 7), stride_w=256)
    assert np.array_equal(arr_int8, unpacked_16)

    # Descriptor-driven packing/unpacking via n1x_dla and cudla_python
    desc_int8 = model_int8.inputs[0]
    packed_desc = pack_tensor_for_desc(arr_int8, desc_int8)
    unpacked_desc = unpack_tensor_from_desc(packed_desc, desc_int8)
    assert np.array_equal(arr_int8, unpacked_desc)

    cudla_desc = cudla_python._descriptor(desc_int8)
    assert np.array_equal(
        arr_int8,
        cudla_python.unpack_tensor(cudla_python.pack_tensor(arr_int8, cudla_desc), cudla_desc),
    )

    # Float quantization/dequantization round-trip
    fp = np.linspace(-1.5, 1.5, 32 * 7 * 7, dtype=np.float32).reshape(1, 32, 7, 7)
    q = quantize_int8(fp, scale=2.0 / 127.0)
    dq = dequantize_int8(q, scale=2.0 / 127.0)
    assert np.max(np.abs(fp - dq)) <= (2.0 / 127.0) * 0.51

    return {"int8_bytes": len(nvda_int8), "qdq_bytes": len(nvda_qdq)}


def main() -> None:
    test_reference_reproduction()
    test_cli_binary()
    dag_stats = test_residual_dag_and_multi_io()
    int8_stats = test_int8_compilation_and_packing()
    print(
        f"Native N1X compiler: 4 exact reference loadables, CLI, residual/multi-IO DAGs "
        f"({dag_stats['residual_bytes']}/{dag_stats['multi_io_bytes']} B), and INT8/QDQ "
        f"({int8_stats['int8_bytes']}/{int8_stats['qdq_bytes']} B) verified"
    )


if __name__ == "__main__":
    main()
