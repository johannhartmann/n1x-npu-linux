"""Native in-process N1X (t254) DAG and ONNX compiler interface.

Wraps runtime/libn1x_compiler.so to compile declarative JSON DAGs and ONNX
models (both FP16 and INT8 with implicit dynamic-range calibration or explicit
QuantizeLinear/DequantizeLinear nodes) directly into .nvda FlatBuffers loadables.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
from pathlib import Path
import struct
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIB_PATH = ROOT / "runtime" / "libn1x_compiler.so"


class CompilerError(RuntimeError):
    """Raised when graph construction or t254 compilation fails."""


def _encode_b64_array(arr: np.ndarray, dtype: str = "fp32") -> tuple[str, str, int]:
    if dtype in ("fp16", "half"):
        raw = np.asarray(arr, dtype="<f2").tobytes()
        return base64.b64encode(raw).decode("ascii"), "fp16", int(np.size(arr))
    if dtype in ("int8",):
        raw = np.asarray(arr, dtype="<i1").tobytes()
        return base64.b64encode(raw).decode("ascii"), "int8", int(np.size(arr))
    raw = np.asarray(arr, dtype="<f4").tobytes()
    return base64.b64encode(raw).decode("ascii"), "fp32", int(np.size(arr))


class CompilerClient:
    """In-process wrapper around runtime/libn1x_compiler.so."""

    def __init__(self, lib_path: Path | str | None = None) -> None:
        path = Path(lib_path) if lib_path else DEFAULT_LIB_PATH
        if not path.is_file():
            raise CompilerError(f"compiler library not found: {path}")
        self._lib = ctypes.CDLL(str(path))
        self._lib.n1x_compiler_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._lib.n1x_compiler_init.restype = ctypes.c_int
        self._lib.n1x_compile_json_graph.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        self._lib.n1x_compile_json_graph.restype = ctypes.c_int
        self._lib.n1x_compiler_free_buffer.argtypes = [ctypes.c_void_p]
        self._lib.n1x_compiler_free_buffer.restype = None

        rc = self._lib.n1x_compiler_init(None, None)
        if rc != 0:
            raise CompilerError(f"n1x_compiler_init failed with status {rc}")

    def compile_dag(
        self,
        graph_spec: dict[str, Any] | str,
        output_path: Path | str | None = None,
    ) -> bytes:
        """Compile a JSON DAG specification into a serialized .nvda loadable."""
        json_text = (
            graph_spec
            if isinstance(graph_spec, str)
            else json.dumps(graph_spec, separators=(",", ":"))
        )
        out_ptr = ctypes.POINTER(ctypes.c_uint8)()
        out_size = ctypes.c_size_t(0)
        err_buf = ctypes.create_string_buffer(512)
        rc = self._lib.n1x_compile_json_graph(
            json_text.encode("utf-8"),
            ctypes.byref(out_ptr),
            ctypes.byref(out_size),
            err_buf,
            ctypes.sizeof(err_buf),
        )
        if rc != 0:
            msg = err_buf.value.decode("utf-8", errors="replace")
            raise CompilerError(f"n1x_compile_json_graph failed ({rc}): {msg}")
        try:
            blob = ctypes.string_at(out_ptr, out_size.value)
        finally:
            self._lib.n1x_compiler_free_buffer(ctypes.cast(out_ptr, ctypes.c_void_p))
        if output_path is not None:
            out_p = Path(output_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(blob)
        return blob


_DEFAULT_CLIENT: CompilerClient | None = None


def get_compiler() -> CompilerClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = CompilerClient()
    return _DEFAULT_CLIENT


def compile_dag(
    graph_spec: dict[str, Any] | str,
    output_path: Path | str | None = None,
) -> bytes:
    """Compile a declarative DAG specification using the shared CompilerClient."""
    return get_compiler().compile_dag(graph_spec, output_path=output_path)


def build_reference_dag(model_name: str, root: Path = ROOT) -> dict[str, Any]:
    """Construct the exact DAG specification for the 4 pinned reference models."""
    if model_name == "relu":
        return {
            "target": "t254",
            "profile_name": "probe",
            "compute_precision": "fp16",
            "inputs": [{"name": "input", "shape": [1, 32, 7, 7]}],
            "layers": [{"op": "Activation", "mode": "relu", "input": "input", "output": "output"}],
            "outputs": [{"name": "output", "tensor_name": "output"}],
        }
    if model_name in ("conv1x1", "conv1x1_relu"):
        kernel_values = [
            (i % 3 + 1) / 4 if j == i else -0.125 if j == (i + 1) % 32 else 0.0
            for i in range(32)
            for j in range(32)
        ]
        bias_values = [i / 32 - 0.5 for i in range(32)]
        k_raw = b"".join(struct.pack("<e", v) for v in kernel_values)
        b_raw = b"".join(struct.pack("<e", v) for v in bias_values)
        conv_out = "conv_out" if model_name == "conv1x1_relu" else "output"
        layers: list[dict[str, Any]] = [
            {
                "op": "Convolution",
                "input": "input",
                "output": conv_out,
                "out_channels": 32,
                "kernel": [1, 1],
                "stride": [1, 1],
                "padding": [0, 0, 0, 0],
                "dilation": [1, 1],
                "groups": 1,
                "bias_mode": "channel",
                "weights_b64": base64.b64encode(k_raw).decode("ascii"),
                "weights_dtype": "fp16",
                "bias_b64": base64.b64encode(b_raw).decode("ascii"),
                "bias_dtype": "fp16",
            }
        ]
        if model_name == "conv1x1_relu":
            layers.append(
                {"op": "Activation", "mode": "relu", "input": "conv_out", "output": "output"}
            )
        return {
            "target": "t254",
            "profile_name": "probe",
            "compute_precision": "fp16",
            "inputs": [{"name": "input", "shape": [1, 32, 7, 7]}],
            "layers": layers,
            "outputs": [{"name": "output", "tensor_name": "output"}],
        }
    if model_name == "superres":
        import onnx
        from onnx import numpy_helper

        path = root / "sources" / "demo-research" / "super-resolution-10.onnx"
        model = onnx.load_model_from_string(path.read_bytes())
        weights = {t.name: numpy_helper.to_array(t) for t in model.graph.initializer}
        layers = []
        prev = "input"
        specs = ((64, 5), (64, 3), (32, 3), (9, 3))
        for idx, (channels, ksize) in enumerate(specs, 1):
            w_arr = np.asarray(weights[f"conv{idx}.weight"], dtype="<f4")
            b_arr = np.asarray(weights[f"conv{idx}.bias"], dtype="<f4")
            pad = ksize // 2
            c_out = f"conv{idx}_out" if idx < 4 else "output"
            layers.append(
                {
                    "op": "Convolution",
                    "input": prev,
                    "output": c_out,
                    "out_channels": channels,
                    "kernel": [ksize, ksize],
                    "stride": [1, 1],
                    "padding": [pad, pad, pad, pad],
                    "dilation": [1, 1],
                    "groups": 1,
                    "bias_mode": "channel",
                    "weights_b64": base64.b64encode(w_arr.tobytes()).decode("ascii"),
                    "weights_dtype": "fp32",
                    "bias_b64": base64.b64encode(b_arr.tobytes()).decode("ascii"),
                    "bias_dtype": "fp32",
                }
            )
            prev = c_out
            if idx < 4:
                r_out = f"relu{idx}_out"
                layers.append(
                    {"op": "Activation", "mode": "relu", "input": prev, "output": r_out}
                )
                prev = r_out
        return {
            "target": "t254",
            "profile_name": "probe",
            "compute_precision": "fp16",
            "inputs": [{"name": "input", "shape": [1, 1, 64, 64]}],
            "layers": layers,
            "outputs": [{"name": "output", "tensor_name": "output"}],
        }
    raise ValueError(f"unknown reference model: {model_name}")


def onnx_to_dag(
    onnx_model_or_path: Any,
    *,
    compute_precision: str = "fp16",
    calibration: dict[str, Any] | None = None,
    explicit_qdq: bool = False,
    input_shapes: dict[str, list[int]] | None = None,
    weight_dtype: str = "fp32",
    include_pixel_shuffle: bool = True,
) -> dict[str, Any]:
    """Lower an ONNX model or path into a declarative N1X JSON DAG specification."""
    import onnx
    from onnx import numpy_helper

    if isinstance(onnx_model_or_path, (str, Path)):
        model = onnx.load(str(onnx_model_or_path))
    elif isinstance(onnx_model_or_path, (bytes, bytearray)):
        model = onnx.load_model_from_string(bytes(onnx_model_or_path))
    else:
        model = onnx_model_or_path

    graph = model.graph
    initializers: dict[str, np.ndarray] = {
        init.name: numpy_helper.to_array(init) for init in graph.initializer
    }

    # Also collect Constant nodes into initializers
    for node in graph.node:
        if node.op_type == "Constant":
            for attr in node.attribute:
                if attr.name == "value":
                    initializers[node.output[0]] = numpy_helper.to_array(attr.t)

    inputs_spec: list[dict[str, Any]] = []
    tensor_shapes: dict[str, list[int]] = {}
    for inp in graph.input:
        if inp.name in initializers:
            continue
        if input_shapes and inp.name in input_shapes:
            shape = list(input_shapes[inp.name])
        else:
            dims = [
                int(d.dim_value) if d.dim_value > 0 else 1
                for d in inp.type.tensor_type.shape.dim
            ]
            shape = dims
        tensor_shapes[inp.name] = shape
        inputs_spec.append({"name": inp.name, "shape": shape})

    layers_spec: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}

    def resolve(name: str) -> str:
        while name in alias_map:
            name = alias_map[name]
        return name

    def get_attrs(node: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for a in node.attribute:
            if a.type == onnx.AttributeProto.INT:
                out[a.name] = int(a.i)
            elif a.type == onnx.AttributeProto.FLOAT:
                out[a.name] = float(a.f)
            elif a.type == onnx.AttributeProto.STRING:
                out[a.name] = a.s.decode("utf-8", errors="replace")
            elif a.type == onnx.AttributeProto.INTS:
                out[a.name] = [int(x) for x in a.ints]
            elif a.type == onnx.AttributeProto.FLOATS:
                out[a.name] = [float(x) for x in a.floats]
        return out

    for node in graph.node:
        op = node.op_type
        if op == "Constant":
            continue
        if op in ("Identity", "Dropout", "Flatten", "Reshape", "Squeeze", "Unsqueeze"):
            src = resolve(node.input[0])
            alias_map[node.output[0]] = src
            if src in tensor_shapes:
                tensor_shapes[node.output[0]] = tensor_shapes[src]
            continue
        if op == "Transpose":
            src = resolve(node.input[0])
            attrs = get_attrs(node)
            perm = list(attrs.get("perm", []))
            # 6D Reshape -> Transpose([0, 1, 4, 2, 5, 3]) -> Reshape is opset-10 PixelShuffle
            if perm == [0, 1, 4, 2, 5, 3]:
                if not include_pixel_shuffle:
                    alias_map[node.output[0]] = src
                    if src in tensor_shapes:
                        tensor_shapes[node.output[0]] = tensor_shapes[src]
                    continue
                in_sh = tensor_shapes.get(src, [1, 9, 64, 64])
                blocksize = int(round(in_sh[1] ** 0.5))
                layers_spec.append(
                    {
                        "op": "PixelShuffle",
                        "input": src,
                        "output": node.output[0],
                        "upscale_factor": blocksize,
                    }
                )
                tensor_shapes[node.output[0]] = [
                    in_sh[0],
                    in_sh[1] // (blocksize * blocksize),
                    in_sh[2] * blocksize,
                    in_sh[3] * blocksize,
                ]
                continue
            alias_map[node.output[0]] = src
            continue

        attrs = get_attrs(node)
        out_name = node.output[0]

        if op in ("Conv", "ConvTranspose"):
            inp_name = resolve(node.input[0])
            w_arr = initializers[node.input[1]]
            b_arr = initializers[node.input[2]] if len(node.input) > 2 and node.input[2] else None
            groups = int(attrs.get("group", 1))
            out_channels = int(w_arr.shape[0]) if op == "Conv" else int(w_arr.shape[1]) * groups
            kshape = list(attrs.get("kernel_shape", w_arr.shape[2:]))
            strides = list(attrs.get("strides", [1, 1]))
            dilations = list(attrs.get("dilations", [1, 1]))
            pads = list(attrs.get("pads", [0, 0, 0, 0]))
            if len(pads) == 2:
                pads = [pads[0], pads[1], pads[0], pads[1]]
            wb64, wdt, _ = _encode_b64_array(w_arr, weight_dtype)
            layer_dict: dict[str, Any] = {
                "op": "Convolution" if op == "Conv" else "Deconvolution",
                "input": inp_name,
                "output": out_name,
                "out_channels": out_channels,
                "kernel": kshape,
                "stride": strides,
                "padding": pads,
                "dilation": dilations,
                "groups": groups,
                "weights_b64": wb64,
                "weights_dtype": wdt,
                "bias_mode": "channel" if b_arr is not None else "none",
            }
            if b_arr is not None:
                bb64, bdt, _ = _encode_b64_array(b_arr, weight_dtype)
                layer_dict["bias_b64"] = bb64
                layer_dict["bias_dtype"] = bdt
            layers_spec.append(layer_dict)
            if inp_name in tensor_shapes:
                n, _, h, w = tensor_shapes[inp_name]
                if op == "Conv":
                    oh = (h + pads[0] + pads[2] - dilations[0] * (kshape[0] - 1) - 1) // strides[0] + 1
                    ow = (w + pads[1] + pads[3] - dilations[1] * (kshape[1] - 1) - 1) // strides[1] + 1
                else:
                    oh = (h - 1) * strides[0] - (pads[0] + pads[2]) + dilations[0] * (kshape[0] - 1) + 1
                    ow = (w - 1) * strides[1] - (pads[1] + pads[3]) + dilations[1] * (kshape[1] - 1) + 1
                tensor_shapes[out_name] = [n, out_channels, oh, ow]

        elif op in ("Gemm", "MatMul"):
            inp_name = resolve(node.input[0])
            w_arr = initializers[node.input[1]]
            trans_b = int(attrs.get("transB", 0)) if op == "Gemm" else 0
            if trans_b == 0 and w_arr.ndim == 2:
                w_arr = w_arr.T
            out_channels = int(w_arr.shape[0])
            b_arr = initializers[node.input[2]] if len(node.input) > 2 and node.input[2] else None
            wb64, wdt, _ = _encode_b64_array(w_arr, weight_dtype)
            layer_dict = {
                "op": "FullyConnected",
                "input": inp_name,
                "output": out_name,
                "out_channels": out_channels,
                "weights_b64": wb64,
                "weights_dtype": wdt,
                "bias_mode": "channel" if b_arr is not None else "none",
            }
            if b_arr is not None:
                bb64, bdt, _ = _encode_b64_array(b_arr, weight_dtype)
                layer_dict["bias_b64"] = bb64
                layer_dict["bias_dtype"] = bdt
            layers_spec.append(layer_dict)
            tensor_shapes[out_name] = [1, out_channels, 1, 1]

        elif op in ("Relu", "Sigmoid", "Tanh", "Clip", "LeakyRelu"):
            inp_name = resolve(node.input[0])
            layer_dict = {
                "op": "Activation",
                "mode": op.lower() if op != "LeakyRelu" else "leaky_relu",
                "input": inp_name,
                "output": out_name,
            }
            if op == "Clip":
                cmin = float(initializers[node.input[1]]) if len(node.input) > 1 and node.input[1] in initializers else float(attrs.get("min", 0.0))
                cmax = float(initializers[node.input[2]]) if len(node.input) > 2 and node.input[2] in initializers else float(attrs.get("max", 6.0))
                layer_dict["alpha"] = [cmin]
                layer_dict["beta"] = [cmax]
            elif op == "LeakyRelu":
                layer_dict["alpha"] = [float(attrs.get("alpha", 0.01))]
            layers_spec.append(layer_dict)
            if inp_name in tensor_shapes:
                tensor_shapes[out_name] = list(tensor_shapes[inp_name])

        elif op in ("MaxPool", "AveragePool", "GlobalAveragePool"):
            inp_name = resolve(node.input[0])
            if op == "GlobalAveragePool":
                in_shape = tensor_shapes.get(inp_name, [1, 16, 1, 1])
                window = [in_shape[2], in_shape[3]]
                strides = [1, 1]
                pads = [0, 0, 0, 0]
                pmode = "average"
            else:
                window = list(attrs.get("kernel_shape", [2, 2]))
                strides = list(attrs.get("strides", window))
                pads = list(attrs.get("pads", [0, 0, 0, 0]))
                if len(pads) == 2:
                    pads = [pads[0], pads[1], pads[0], pads[1]]
                pmode = "max" if op == "MaxPool" else "average"
            layers_spec.append(
                {
                    "op": "Pooling",
                    "mode": pmode,
                    "input": inp_name,
                    "output": out_name,
                    "window": window,
                    "stride": strides,
                    "padding": pads,
                }
            )
            if inp_name in tensor_shapes:
                n, c, h, w = tensor_shapes[inp_name]
                oh = (h + pads[0] + pads[2] - window[0]) // strides[0] + 1
                ow = (w + pads[1] + pads[3] - window[1]) // strides[1] + 1
                tensor_shapes[out_name] = [n, c, oh, ow]

        elif op in ("Add", "Sub", "Mul", "Max", "Min"):
            in0 = resolve(node.input[0])
            in1 = resolve(node.input[1])
            if in1 in initializers and op in ("Add", "Mul"):
                const_arr = initializers[in1].reshape(-1)
                c_len = int(const_arr.size)
                shift = const_arr if op == "Add" else np.zeros(c_len, dtype=np.float32)
                scale = const_arr if op == "Mul" else np.ones(c_len, dtype=np.float32)
                sb64, sdt, _ = _encode_b64_array(shift, weight_dtype)
                scb64, scdt, _ = _encode_b64_array(scale, weight_dtype)
                layers_spec.append(
                    {
                        "op": "Scale",
                        "mode": "channel" if c_len > 1 else "uniform",
                        "input": in0,
                        "output": out_name,
                        "shift_b64": sb64,
                        "shift_dtype": sdt,
                        "scale_b64": scb64,
                        "scale_dtype": scdt,
                    }
                )
            else:
                layers_spec.append(
                    {
                        "op": "ElementWise",
                        "mode": op.lower(),
                        "inputs": [in0, in1],
                        "output": out_name,
                    }
                )
            if in0 in tensor_shapes:
                tensor_shapes[out_name] = list(tensor_shapes[in0])

        elif op == "BatchNormalization":
            inp_name = resolve(node.input[0])
            scale = np.asarray(initializers[node.input[1]], dtype=np.float64).reshape(-1)
            bias = np.asarray(initializers[node.input[2]], dtype=np.float64).reshape(-1)
            mean = np.asarray(initializers[node.input[3]], dtype=np.float64).reshape(-1)
            var = np.asarray(initializers[node.input[4]], dtype=np.float64).reshape(-1)
            eps = float(attrs.get("epsilon", 1e-5))
            # Lower BatchNormalization with affine scale/bias into Scale(channel):
            # y = (x - mean) / sqrt(var + eps) * scale + bias = x * eff_scale + eff_shift
            eff_scale = (scale / np.sqrt(var + eps)).astype(np.float32)
            eff_shift = (bias - mean * eff_scale).astype(np.float32)
            sb64, sdt, _ = _encode_b64_array(eff_shift, weight_dtype)
            scb64, scdt, _ = _encode_b64_array(eff_scale, weight_dtype)
            layers_spec.append(
                {
                    "op": "Scale",
                    "mode": "channel",
                    "input": inp_name,
                    "output": out_name,
                    "shift_b64": sb64,
                    "shift_dtype": sdt,
                    "scale_b64": scb64,
                    "scale_dtype": scdt,
                }
            )
            if inp_name in tensor_shapes:
                tensor_shapes[out_name] = list(tensor_shapes[inp_name])

        elif op == "Concat":
            cat_ins = [resolve(x) for x in node.input]
            axis = int(attrs.get("axis", 1))
            layers_spec.append(
                {
                    "op": "Concatenation",
                    "inputs": cat_ins,
                    "axis": axis,
                    "output": out_name,
                }
            )
            if cat_ins[0] in tensor_shapes:
                sh = list(tensor_shapes[cat_ins[0]])
                sh[axis] = sum(tensor_shapes[x][axis] for x in cat_ins if x in tensor_shapes)
                tensor_shapes[out_name] = sh

        elif op == "DepthToSpace":
            inp_name = resolve(node.input[0])
            if not include_pixel_shuffle:
                alias_map[out_name] = inp_name
                continue
            blocksize = int(attrs.get("blocksize", 2))
            layers_spec.append(
                {
                    "op": "PixelShuffle",
                    "input": inp_name,
                    "output": out_name,
                    "upscale_factor": blocksize,
                }
            )
            if inp_name in tensor_shapes:
                n, c, h, w = tensor_shapes[inp_name]
                tensor_shapes[out_name] = [
                    n,
                    c // (blocksize * blocksize),
                    h * blocksize,
                    w * blocksize,
                ]

        elif op in ("QuantizeLinear", "DequantizeLinear"):
            inp_name = resolve(node.input[0])
            sc_arr = np.asarray(initializers[node.input[1]], dtype=np.float32).reshape(-1)
            if len(node.input) > 2 and node.input[2] in initializers:
                zp_arr = np.asarray(initializers[node.input[2]], dtype=np.int8).reshape(-1)
            else:
                zp_arr = np.zeros_like(sc_arr, dtype=np.int8)
            axis = int(attrs.get("axis", 1))
            scb64, scdt, _ = _encode_b64_array(sc_arr, "fp32")
            zpb64, zpdt, _ = _encode_b64_array(zp_arr, "int8")
            layers_spec.append(
                {
                    "op": "Quantize" if op == "QuantizeLinear" else "Dequantize",
                    "input": inp_name,
                    "output": out_name,
                    "axis": axis,
                    "scales_b64": scb64,
                    "scales_dtype": scdt,
                    "zero_points_b64": zpb64,
                    "zero_points_dtype": zpdt,
                }
            )
            if inp_name in tensor_shapes:
                tensor_shapes[out_name] = list(tensor_shapes[inp_name])

        else:
            raise CompilerError(f"unsupported ONNX operator in graph: {op}")

    outputs_spec: list[dict[str, Any]] = []
    for out in graph.output:
        resolved_out = resolve(out.name)
        outputs_spec.append(
            {
                "name": resolved_out,
                "tensor_name": "output" if len(graph.output) == 1 else out.name,
            }
        )

    dag: dict[str, Any] = {
        "target": "t254",
        "profile_name": "probe",
        "compute_precision": compute_precision,
        "explicit_qdq": explicit_qdq,
        "inputs": inputs_spec,
        "layers": layers_spec,
        "outputs": outputs_spec,
    }
    if calibration:
        dag["calibration"] = calibration
    return dag


def compile_onnx(
    onnx_model_or_path: Any,
    output_path: Path | str | None = None,
    *,
    compute_precision: str = "fp16",
    calibration: dict[str, Any] | None = None,
    explicit_qdq: bool = False,
    input_shapes: dict[str, list[int]] | None = None,
    include_pixel_shuffle: bool = True,
) -> bytes:
    """Compile an ONNX model directly into a .nvda loadable."""
    dag = onnx_to_dag(
        onnx_model_or_path,
        compute_precision=compute_precision,
        calibration=calibration,
        explicit_qdq=explicit_qdq,
        input_shapes=input_shapes,
        include_pixel_shuffle=include_pixel_shuffle,
    )
    return compile_dag(dag, output_path=output_path)
