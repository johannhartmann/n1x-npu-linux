"""N1X C-Backed Heterogeneous ONNX Execution Provider and Graph Partitioner.

Provides:
- Native C/C++ surface packing/unpacking and retained-job execution bindings
  (`N1xOrtClient`, `N1xOrtModuleWrapper`) over `runtime/libn1x_ort.so`.
- Canonical ONNX graph rewrites (`canonicalize_onnx_model`) fusing:
  - `Conv + Add(const)` -> biased `Conv`
  - `Flatten/Reshape + MatMul + Add(const)` -> `Gemm` (`FullyConnected`)
  - Opset-10 decomposed sub-pixel `Reshape -> Transpose -> Reshape` -> `DepthToSpace`
  - Constant/initializer subgraph folding (`Reshape` on initializers, etc.)
- Multi-sample Post-Training Quantization (`INT8`) activation range collector
  (`calibrate_onnx_model`).
- Cycle-free topological graph partitioner (`partition_onnx_model`) with SHA-256
  `.nvda` disk caching and compiler-verified boundary fallback.
- `onnxruntime.InferenceSession`-compatible `N1xInferenceSession` (`InferenceSession`).
"""
from __future__ import annotations

import copy
import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import onnx
from onnx import helper, numpy_helper, TensorProto
from onnx.reference import ReferenceEvaluator

from n1x_compiler import CompilerError, compile_dag, onnx_to_dag

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORT_LIB_PATH = (
    ROOT / "runtime" / "libn1x_ort.so"
    if (ROOT / "runtime" / "libn1x_ort.so").is_file()
    else Path("/usr/lib/libn1x_ort.so")
)
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "n1x" / "ort"


class N1xOrtError(RuntimeError):
    """Raised on N1X ONNX Execution Provider compilation or runtime errors."""


class C_N1xOrtTensorDesc(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * 88),
        ("size", ctypes.c_uint64),
        ("n", ctypes.c_uint32),
        ("c", ctypes.c_uint32),
        ("h", ctypes.c_uint32),
        ("w", ctypes.c_uint32),
        ("data_format", ctypes.c_uint8),
        ("data_type", ctypes.c_uint8),
        ("data_category", ctypes.c_uint8),
        ("pixel_format", ctypes.c_uint8),
        ("pixel_mapping", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
        ("stride", ctypes.c_uint32 * 8),
    ]


@dataclass(frozen=True)
class TensorDesc:
    name: str
    size: int
    shape: tuple[int, int, int, int]
    data_format: int
    data_type: int
    data_category: int
    pixel_format: int
    pixel_mapping: int
    strides: tuple[int, ...]

    @classmethod
    def from_ctypes(cls, raw: C_N1xOrtTensorDesc) -> TensorDesc:
        return cls(
            name=raw.name.decode("utf-8", errors="replace"),
            size=int(raw.size),
            shape=(int(raw.n), int(raw.c), int(raw.h), int(raw.w)),
            data_format=int(raw.data_format),
            data_type=int(raw.data_type),
            data_category=int(raw.data_category),
            pixel_format=int(raw.pixel_format),
            pixel_mapping=int(raw.pixel_mapping),
            strides=tuple(int(raw.stride[i]) for i in range(8)),
        )

    def to_ctypes(self) -> C_N1xOrtTensorDesc:
        c_desc = C_N1xOrtTensorDesc()
        c_desc.name = self.name.encode("utf-8")[:87]
        c_desc.size = self.size
        c_desc.n, c_desc.c, c_desc.h, c_desc.w = self.shape
        c_desc.data_format = self.data_format
        c_desc.data_type = self.data_type
        c_desc.data_category = self.data_category
        c_desc.pixel_format = self.pixel_format
        c_desc.pixel_mapping = self.pixel_mapping
        for i in range(min(8, len(self.strides))):
            c_desc.stride[i] = self.strides[i]
        return c_desc


class N1xOrtClient:
    """ctypes wrapper around runtime/libn1x_ort.so."""

    def __init__(self, lib_path: Path | str | None = None) -> None:
        path = Path(lib_path) if lib_path else DEFAULT_ORT_LIB_PATH
        if not path.is_file():
            raise N1xOrtError(f"libn1x_ort.so not found at {path}")
        lib = self._lib = ctypes.CDLL(str(path))

        lib.n1x_ort_module_create.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.n1x_ort_module_create.restype = ctypes.c_int

        lib.n1x_ort_module_create_from_file.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.n1x_ort_module_create_from_file.restype = ctypes.c_int

        lib.n1x_ort_module_destroy.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_destroy.restype = None

        lib.n1x_ort_module_num_inputs.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_num_inputs.restype = ctypes.c_size_t

        lib.n1x_ort_module_num_outputs.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_num_outputs.restype = ctypes.c_size_t

        lib.n1x_ort_module_get_input_desc.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(C_N1xOrtTensorDesc),
        ]
        lib.n1x_ort_module_get_input_desc.restype = ctypes.c_int

        lib.n1x_ort_module_get_output_desc.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(C_N1xOrtTensorDesc),
        ]
        lib.n1x_ort_module_get_output_desc.restype = ctypes.c_int

        lib.n1x_ort_module_run_count.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_run_count.restype = ctypes.c_uint64

        lib.n1x_ort_module_used_resubmit.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_used_resubmit.restype = ctypes.c_int

        lib.n1x_ort_module_used_mmap.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_used_mmap.restype = ctypes.c_int

        lib.n1x_ort_module_has_stats.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_has_stats.restype = ctypes.c_int

        lib.n1x_ort_module_stats_bytes.argtypes = [ctypes.c_void_p]
        lib.n1x_ort_module_stats_bytes.restype = ctypes.c_size_t

        lib.n1x_ort_module_read_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.n1x_ort_module_read_stats.restype = ctypes.c_int

        lib.n1x_ort_module_set_input_quant.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int8),
            ctypes.c_size_t,
        ]
        lib.n1x_ort_module_set_input_quant.restype = ctypes.c_int

        lib.n1x_ort_module_set_output_quant.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int8),
            ctypes.c_size_t,
        ]
        lib.n1x_ort_module_set_output_quant.restype = ctypes.c_int

        lib.n1x_ort_pack_fp32.argtypes = [
            ctypes.POINTER(C_N1xOrtTensorDesc),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
        ]
        lib.n1x_ort_pack_fp32.restype = ctypes.c_int

        lib.n1x_ort_unpack_fp32.argtypes = [
            ctypes.POINTER(C_N1xOrtTensorDesc),
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
        ]
        lib.n1x_ort_unpack_fp32.restype = ctypes.c_int

        lib.n1x_ort_module_run_fp32.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.n1x_ort_module_run_fp32.restype = ctypes.c_int

    def pack_fp32(
        self,
        desc: TensorDesc | C_N1xOrtTensorDesc,
        arr: np.ndarray,
        scale: float | np.ndarray | None = None,
        zero_point: int | np.ndarray = 0,
    ) -> bytes:
        c_desc = desc.to_ctypes() if isinstance(desc, TensorDesc) else desc
        fp32 = np.ascontiguousarray(arr, dtype=np.float32)
        s_arr = (
            np.ascontiguousarray(scale, dtype=np.float32).reshape(-1)
            if scale is not None
            else np.array([1.0], dtype=np.float32)
        )
        zp_arr = np.ascontiguousarray(zero_point, dtype=np.int8).reshape(-1)
        out_buf = (ctypes.c_uint8 * int(c_desc.size))()
        rc = self._lib.n1x_ort_pack_fp32(
            ctypes.byref(c_desc),
            fp32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            fp32.size,
            s_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            s_arr.size,
            zp_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int8)),
            zp_arr.size,
            out_buf,
            int(c_desc.size),
        )
        if rc != 0:
            if rc == -errno.EINVAL:
                raise ValueError("n1x_ort_pack_fp32 rejected invalid or non-finite input")
            raise N1xOrtError(f"n1x_ort_pack_fp32 failed with status {rc}")
        return bytes(out_buf)

    def unpack_fp32(
        self,
        desc: TensorDesc | C_N1xOrtTensorDesc,
        surface_bytes: bytes | bytearray,
        scale: float | np.ndarray | None = None,
        zero_point: int | np.ndarray = 0,
    ) -> np.ndarray:
        c_desc = desc.to_ctypes() if isinstance(desc, TensorDesc) else desc
        shape = (int(c_desc.n), int(c_desc.c), int(c_desc.h), int(c_desc.w))
        raw = bytes(surface_bytes)
        if len(raw) != int(c_desc.size):
            raise ValueError("packed tensor size mismatch")
        s_arr = (
            np.ascontiguousarray(scale, dtype=np.float32).reshape(-1)
            if scale is not None
            else np.array([1.0], dtype=np.float32)
        )
        zp_arr = np.ascontiguousarray(zero_point, dtype=np.int8).reshape(-1)
        out = np.empty(shape, dtype=np.float32)
        in_buf = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw)
        rc = self._lib.n1x_ort_unpack_fp32(
            ctypes.byref(c_desc),
            in_buf,
            len(raw),
            s_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            s_arr.size,
            zp_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int8)),
            zp_arr.size,
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            out.size,
        )
        if rc != 0:
            if rc == -errno.EINVAL:
                raise ValueError("n1x_ort_unpack_fp32 rejected invalid or non-finite surface")
            raise N1xOrtError(f"n1x_ort_unpack_fp32 failed with status {rc}")
        return out


_DEFAULT_ORT_CLIENT: N1xOrtClient | None = None


def get_ort_client() -> N1xOrtClient:
    global _DEFAULT_ORT_CLIENT
    if _DEFAULT_ORT_CLIENT is None:
        _DEFAULT_ORT_CLIENT = N1xOrtClient()
    return _DEFAULT_ORT_CLIENT


class N1xOrtModuleWrapper:
    """RAII wrapper around a C `N1xOrtModule*` instance."""

    def __init__(
        self,
        nvda_bytes: bytes,
        accel_fd: int = -1,
        client: N1xOrtClient | None = None,
        device_path: Path | str | None = None,
    ) -> None:
        self._client = client or get_ort_client()
        self._lib = self._client._lib
        self._handle = ctypes.c_void_p(0)
        self._device_path = str(device_path) if device_path is not None else None
        buf = (ctypes.c_uint8 * len(nvda_bytes)).from_buffer_copy(nvda_bytes)
        err_buf = ctypes.create_string_buffer(256)
        rc = self._lib.n1x_ort_module_create(
            buf,
            len(nvda_bytes),
            int(accel_fd),
            ctypes.byref(self._handle),
            err_buf,
            ctypes.sizeof(err_buf),
        )
        if rc != 0 or not self._handle.value:
            msg = err_buf.value.decode("utf-8", errors="replace")
            raise N1xOrtError(f"n1x_ort_module_create failed ({rc}): {msg}")

        self.inputs: list[TensorDesc] = []
        n_in = int(self._lib.n1x_ort_module_num_inputs(self._handle))
        for i in range(n_in):
            d = C_N1xOrtTensorDesc()
            self._lib.n1x_ort_module_get_input_desc(self._handle, i, ctypes.byref(d))
            self.inputs.append(TensorDesc.from_ctypes(d))

        self.outputs: list[TensorDesc] = []
        n_out = int(self._lib.n1x_ort_module_num_outputs(self._handle))
        for i in range(n_out):
            d = C_N1xOrtTensorDesc()
            self._lib.n1x_ort_module_get_output_desc(self._handle, i, ctypes.byref(d))
            self.outputs.append(TensorDesc.from_ctypes(d))

    def close(self) -> None:
        if getattr(self, "_handle", None) and self._handle.value:
            self._lib.n1x_ort_module_destroy(self._handle)
            self._handle = ctypes.c_void_p(0)

    def __del__(self) -> None:
        self.close()

    @property
    def run_count(self) -> int:
        return int(self._lib.n1x_ort_module_run_count(self._handle))

    @property
    def used_resubmit(self) -> bool:
        return bool(self._lib.n1x_ort_module_used_resubmit(self._handle))

    @property
    def used_mmap(self) -> bool:
        return bool(self._lib.n1x_ort_module_used_mmap(self._handle))

    @property
    def has_stats(self) -> bool:
        return bool(self._lib.n1x_ort_module_has_stats(self._handle))

    @property
    def stats_bytes(self) -> int:
        return int(self._lib.n1x_ort_module_stats_bytes(self._handle))

    def read_stats(self) -> bytes:
        nbytes = self.stats_bytes
        if not self.has_stats or nbytes == 0:
            raise N1xOrtError("module does not contain a task-0-stats profiling surface")
        out_buf = (ctypes.c_uint8 * nbytes)()
        actual = ctypes.c_size_t(0)
        rc = self._lib.n1x_ort_module_read_stats(
            self._handle, out_buf, nbytes, ctypes.byref(actual)
        )
        if rc != 0:
            raise N1xOrtError(f"n1x_ort_module_read_stats failed ({rc})")
        return bytes(out_buf[: int(actual.value)])

    def set_input_quant(
        self, index: int, scale: float | np.ndarray, zero_point: int | np.ndarray = 0
    ) -> None:
        s_arr = np.ascontiguousarray(scale, dtype=np.float32).reshape(-1)
        zp_arr = np.ascontiguousarray(zero_point, dtype=np.int8).reshape(-1)
        rc = self._lib.n1x_ort_module_set_input_quant(
            self._handle,
            int(index),
            s_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            s_arr.size,
            zp_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int8)),
            zp_arr.size,
        )
        if rc != 0:
            raise N1xOrtError(f"n1x_ort_module_set_input_quant failed ({rc})")

    def set_output_quant(
        self, index: int, scale: float | np.ndarray, zero_point: int | np.ndarray = 0
    ) -> None:
        s_arr = np.ascontiguousarray(scale, dtype=np.float32).reshape(-1)
        zp_arr = np.ascontiguousarray(zero_point, dtype=np.int8).reshape(-1)
        rc = self._lib.n1x_ort_module_set_output_quant(
            self._handle,
            int(index),
            s_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            s_arr.size,
            zp_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int8)),
            zp_arr.size,
        )
        if rc != 0:
            raise N1xOrtError(f"n1x_ort_module_set_output_quant failed ({rc})")

    def run_fp32(self, inputs: Sequence[np.ndarray]) -> list[np.ndarray]:
        if len(inputs) != len(self.inputs):
            raise ValueError(f"expected {len(self.inputs)} inputs, got {len(inputs)}")
        in_arrays = [np.ascontiguousarray(arr, dtype=np.float32) for arr in inputs]
        n_in = len(in_arrays)
        in_ptrs = (ctypes.POINTER(ctypes.c_float) * n_in)(
            *(a.ctypes.data_as(ctypes.POINTER(ctypes.c_float)) for a in in_arrays)
        )
        in_counts = (ctypes.c_size_t * n_in)(*(a.size for a in in_arrays))

        out_arrays = [np.empty(d.shape, dtype=np.float32) for d in self.outputs]
        n_out = len(out_arrays)
        out_ptrs = (ctypes.POINTER(ctypes.c_float) * n_out)(
            *(a.ctypes.data_as(ctypes.POINTER(ctypes.c_float)) for a in out_arrays)
        )
        out_counts = (ctypes.c_size_t * n_out)(*(a.size for a in out_arrays))
        err_buf = ctypes.create_string_buffer(256)

        prev_env = os.environ.get("N1X_DLA_DEVICE")
        if self._device_path is not None:
            os.environ["N1X_DLA_DEVICE"] = self._device_path
        try:
            rc = self._lib.n1x_ort_module_run_fp32(
                self._handle,
                in_ptrs,
                in_counts,
                n_in,
                out_ptrs,
                out_counts,
                n_out,
                err_buf,
                ctypes.sizeof(err_buf),
            )
        finally:
            if self._device_path is not None:
                if prev_env is None:
                    os.environ.pop("N1X_DLA_DEVICE", None)
                else:
                    os.environ["N1X_DLA_DEVICE"] = prev_env
        if rc != 0:
            msg = err_buf.value.decode("utf-8", errors="replace")
            raise N1xOrtError(f"n1x_ort_module_run_fp32 failed ({rc}): {msg}")
        return out_arrays

    run = run_fp32


def _load_onnx_model(path_or_bytes: Any) -> onnx.ModelProto:
    if isinstance(path_or_bytes, onnx.ModelProto):
        model = copy.deepcopy(path_or_bytes)
    elif isinstance(path_or_bytes, (str, Path)):
        model = onnx.load(str(path_or_bytes))
    elif isinstance(path_or_bytes, (bytes, bytearray)):
        model = onnx.load_model_from_string(bytes(path_or_bytes))
    else:
        raise TypeError(f"unsupported ONNX model source: {type(path_or_bytes)}")
    return model


def _extract_attr_dict(node: onnx.NodeProto) -> dict[str, Any]:
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
        elif a.type == onnx.AttributeProto.TENSOR:
            out[a.name] = numpy_helper.to_array(a.t)
    return out


def _collect_shape_map(graph: onnx.GraphProto) -> dict[str, list[int]]:
    shapes: dict[str, list[int]] = {}
    for init in graph.initializer:
        shapes[init.name] = list(init.dims)
    for vi in [*graph.input, *graph.value_info, *graph.output]:
        if vi.name in shapes:
            continue
        if vi.type.HasField("tensor_type") and vi.type.tensor_type.HasField("shape"):
            dims = [
                int(d.dim_value) if d.dim_value > 0 else 1
                for d in vi.type.tensor_type.shape.dim
            ]
            if dims:
                shapes[vi.name] = dims
    return shapes


def canonicalize_onnx_model(
    model_or_path: Any,
    input_shapes: Mapping[str, Sequence[int]] | None = None,
) -> tuple[onnx.ModelProto, dict[str, list[int]]]:
    """Canonicalize an ONNX model via static shape binding, constant folding, and pattern fusion."""
    model = _load_onnx_model(model_or_path)
    graph = model.graph

    init_names = {init.name for init in graph.initializer}
    for inp in graph.input:
        if inp.name in init_names:
            continue
        if input_shapes and inp.name in input_shapes:
            target_dims = [int(x) for x in input_shapes[inp.name]]
        else:
            target_dims = [
                int(d.dim_value) if d.dim_value > 0 else 1
                for d in inp.type.tensor_type.shape.dim
            ]
        del inp.type.tensor_type.shape.dim[:]
        for d_val in target_dims:
            dim = inp.type.tensor_type.shape.dim.add()
            dim.dim_value = int(d_val)

    # Clear fixed output shapes that might conflict with overridden input_shapes before infer_shapes
    if input_shapes:
        for out in graph.output:
            if out.type.HasField("tensor_type") and out.type.tensor_type.HasField("shape"):
                del out.type.tensor_type.shape.dim[:]
        del graph.value_info[:]

    try:
        model = onnx.shape_inference.infer_shapes(model)
    except Exception:
        pass
    graph = model.graph

    # Pass 2: Fold Constant nodes and initializer-only subgraphs (e.g., Reshape on Parameter193)
    const_map: dict[str, np.ndarray] = {
        init.name: numpy_helper.to_array(init) for init in graph.initializer
    }
    remaining_nodes: list[onnx.NodeProto] = []
    for node in graph.node:
        if node.op_type == "Constant":
            attrs = _extract_attr_dict(node)
            if "value" in attrs:
                const_map[node.output[0]] = np.asarray(attrs["value"])
            continue
        if node.input and all((inp == "" or inp in const_map) for inp in node.input):
            if node.op_type == "Reshape" and len(node.input) == 2:
                data = const_map[node.input[0]]
                target_shape = [int(x) for x in const_map[node.input[1]].reshape(-1)]
                const_map[node.output[0]] = data.reshape(target_shape)
                continue
            if node.op_type == "Transpose":
                attrs = _extract_attr_dict(node)
                perm = attrs.get("perm")
                const_map[node.output[0]] = np.transpose(const_map[node.input[0]], axes=perm)
                continue
            if node.op_type == "Unsqueeze" and len(node.input) >= 1:
                attrs = _extract_attr_dict(node)
                axes = (
                    [int(x) for x in const_map[node.input[1]].reshape(-1)]
                    if len(node.input) > 1
                    else list(attrs.get("axes", [0]))
                )
                arr = const_map[node.input[0]]
                for ax in sorted(axes):
                    arr = np.expand_dims(arr, axis=ax)
                const_map[node.output[0]] = arr
                continue
        remaining_nodes.append(node)

    del graph.node[:]
    graph.node.extend(remaining_nodes)

    # Rebuild consumer map helper
    def _build_consumers(nodes: Sequence[onnx.NodeProto]) -> dict[str, list[int]]:
        cons: dict[str, list[int]] = {}
        for idx, n in enumerate(nodes):
            for inp in n.input:
                if inp:
                    cons.setdefault(inp, []).append(idx)
        return cons

    graph_output_names = {o.name for o in graph.output}

    # Pass 3: Fuse Conv(X, W) + Add(conv_out, B_const) -> Conv(X, W, B_flat)
    nodes = list(graph.node)
    consumers = _build_consumers(nodes)
    skip_indices: set[int] = set()
    for idx, node in enumerate(nodes):
        if idx in skip_indices:
            continue
        if node.op_type in ("Conv", "ConvTranspose") and (
            len(node.input) == 2 or (len(node.input) == 3 and not node.input[2])
        ):
            conv_out = node.output[0]
            if conv_out in graph_output_names:
                continue
            cons = consumers.get(conv_out, [])
            if len(cons) != 1:
                continue
            add_idx = cons[0]
            add_node = nodes[add_idx]
            if add_node.op_type != "Add" or len(add_node.input) != 2:
                continue
            other_in = (
                add_node.input[1]
                if add_node.input[0] == conv_out
                else add_node.input[0]
                if add_node.input[1] == conv_out
                else None
            )
            if other_in is None or other_in not in const_map:
                continue
            w_arr = const_map.get(node.input[1])
            b_arr = const_map[other_in]
            if w_arr is None:
                continue
            attrs = _extract_attr_dict(node)
            groups = int(attrs.get("group", 1))
            out_ch = int(w_arr.shape[0]) if node.op_type == "Conv" else int(w_arr.shape[1]) * groups
            # Check broadcast compatibility to [1, out_ch, 1, 1]
            if b_arr.size != out_ch:
                continue
            non_unit = [d for d in b_arr.shape if d != 1]
            if len(non_unit) > 1 or (len(non_unit) == 1 and non_unit[0] != out_ch):
                continue
            bias_name = f"{other_in}_flat_bias_{idx}"
            const_map[bias_name] = np.asarray(b_arr, dtype=np.float32).reshape(out_ch)
            if len(node.input) == 2:
                node.input.append(bias_name)
            else:
                node.input[2] = bias_name
            node.output[0] = add_node.output[0]
            skip_indices.add(add_idx)

    nodes = [n for i, n in enumerate(nodes) if i not in skip_indices]

    # Re-run shape inference to ensure intermediate shapes before MatMul/PixelShuffle are populated
    del graph.node[:]
    graph.node.extend(nodes)
    try:
        model = onnx.shape_inference.infer_shapes(model)
    except Exception:
        pass
    graph = model.graph
    shape_map = _collect_shape_map(graph)

    # Pass 4: Fuse [Flatten | Reshape](X_4d) -> MatMul(flat, W_const) [-> Add(mm, B_const)] -> Gemm
    nodes = list(graph.node)
    consumers = _build_consumers(nodes)
    producers = {n.output[0]: i for i, n in enumerate(nodes) if len(n.output) == 1}
    skip_indices = set()
    for idx, node in enumerate(nodes):
        if idx in skip_indices or node.op_type != "MatMul":
            continue
        in_a, in_b = node.input[0], node.input[1]
        if in_b not in const_map:
            continue
        w_arr = const_map[in_b]
        if w_arr.ndim != 2:
            continue
        k_in, k_out = int(w_arr.shape[0]), int(w_arr.shape[1])

        # Check if preceded by Flatten or 2D Reshape of a 4D tensor [1, C, H, W] with C*H*W == k_in
        src_tensor = in_a
        reshape_idx = producers.get(in_a)
        if (
            reshape_idx is not None
            and nodes[reshape_idx].op_type in ("Reshape", "Flatten")
            and in_a not in graph_output_names
            and len(consumers.get(in_a, [])) == 1
        ):
            pre_tensor = nodes[reshape_idx].input[0]
            pre_shape = shape_map.get(pre_tensor)
            if (
                pre_shape
                and len(pre_shape) == 4
                and pre_shape[0] == 1
                and int(np.prod(pre_shape[1:])) == k_in
            ):
                src_tensor = pre_tensor
                skip_indices.add(reshape_idx)

        # Check if followed by Add(mm_out, B_const)
        mm_out = node.output[0]
        final_out = mm_out
        bias_name: str | None = None
        if mm_out not in graph_output_names and len(consumers.get(mm_out, [])) == 1:
            add_idx = consumers[mm_out][0]
            add_node = nodes[add_idx]
            if add_node.op_type == "Add" and len(add_node.input) == 2:
                other_in = (
                    add_node.input[1]
                    if add_node.input[0] == mm_out
                    else add_node.input[0]
                    if add_node.input[1] == mm_out
                    else None
                )
                if other_in is not None and other_in in const_map:
                    b_arr = const_map[other_in]
                    if b_arr.size == k_out:
                        bias_name = f"{other_in}_gemm_bias_{idx}"
                        const_map[bias_name] = np.asarray(b_arr, dtype=np.float32).reshape(k_out)
                        final_out = add_node.output[0]
                        skip_indices.add(add_idx)

        gemm_inputs = [src_tensor, in_b] + ([bias_name] if bias_name else [])
        gemm_node = helper.make_node(
            "Gemm",
            inputs=gemm_inputs,
            outputs=[final_out],
            name=node.name or f"FusedGemm_{idx}",
            alpha=1.0,
            beta=1.0,
            transA=0,
            transB=0,
        )
        nodes[idx] = gemm_node
        shape_map[final_out] = [1, k_out]

    nodes = [n for i, n in enumerate(nodes) if i not in skip_indices]

    # Pass 5: Fuse opset-10 decomposed sub-pixel Reshape -> Transpose([0,1,4,2,5,3]) -> Reshape -> DepthToSpace
    consumers = _build_consumers(nodes)
    producers = {n.output[0]: i for i, n in enumerate(nodes) if len(n.output) == 1}
    skip_indices = set()
    for idx, node in enumerate(nodes):
        if idx in skip_indices or node.op_type != "Transpose":
            continue
        attrs = _extract_attr_dict(node)
        perm = list(attrs.get("perm", []))
        if perm != [0, 1, 4, 2, 5, 3]:
            continue
        r1_out = node.input[0]
        t_out = node.output[0]
        r1_idx = producers.get(r1_out)
        t_cons = consumers.get(t_out, [])
        if (
            r1_idx is None
            or nodes[r1_idx].op_type != "Reshape"
            or len(t_cons) != 1
            or nodes[t_cons[0]].op_type != "Reshape"
        ):
            continue
        r2_idx = t_cons[0]
        r1_node = nodes[r1_idx]
        r2_node = nodes[r2_idx]
        if len(r1_node.input) < 2 or r1_node.input[1] not in const_map:
            continue
        r1_shape = [int(x) for x in const_map[r1_node.input[1]].reshape(-1)]
        if len(r1_shape) != 6 or r1_shape[2] != r1_shape[3] or r1_shape[2] < 2:
            continue
        blocksize = int(r1_shape[2])
        src_x = r1_node.input[0]
        dst_y = r2_node.output[0]
        d2s_node = helper.make_node(
            "DepthToSpace",
            inputs=[src_x],
            outputs=[dst_y],
            name=node.name or f"FusedDepthToSpace_{idx}",
            blocksize=blocksize,
            mode="DCR",
        )
        nodes[idx] = d2s_node
        skip_indices.add(r1_idx)
        skip_indices.add(r2_idx)
        if src_x in shape_map and len(shape_map[src_x]) == 4:
            n_b, c_in, h_in, w_in = shape_map[src_x]
            shape_map[dst_y] = [
                n_b,
                c_in // (blocksize * blocksize),
                h_in * blocksize,
                w_in * blocksize,
            ]

    nodes = [n for i, n in enumerate(nodes) if i not in skip_indices]

    # Prune unreferenced initializers and rebuild graph.initializer from const_map
    used_inputs = {inp for n in nodes for inp in n.input if inp}
    del graph.initializer[:]
    for name, arr in const_map.items():
        if name in used_inputs:
            graph.initializer.append(numpy_helper.from_array(arr, name=name))

    # Also ensure graph.input only contains actual dynamic inputs (or referenced initializers)
    kept_inputs = [
        inp for inp in graph.input if inp.name not in const_map or inp.name in used_inputs
    ]
    del graph.input[:]
    graph.input.extend(kept_inputs)

    del graph.node[:]
    graph.node.extend(nodes)

    # Propagate shapes across all nodes in topological order
    _propagate_shapes(graph, const_map, shape_map)
    return model, shape_map


def _propagate_shapes(
    graph: onnx.GraphProto,
    const_map: Mapping[str, np.ndarray],
    shape_map: dict[str, list[int]],
) -> None:
    for node in graph.node:
        op = node.op_type
        attrs = _extract_attr_dict(node)
        out = node.output[0]
        in0 = node.input[0] if node.input else ""
        sh0 = shape_map.get(in0)
        if op in ("Conv", "ConvTranspose") and sh0 and len(sh0) == 4:
            w_arr = const_map[node.input[1]]
            groups = int(attrs.get("group", 1))
            out_ch = int(w_arr.shape[0]) if op == "Conv" else int(w_arr.shape[1]) * groups
            kshape = list(attrs.get("kernel_shape", w_arr.shape[2:]))
            strides = list(attrs.get("strides", [1, 1]))
            dilations = list(attrs.get("dilations", [1, 1]))
            auto_pad = str(attrs.get("auto_pad", "NOTSET"))
            n_b, _, h, w = sh0
            if "pads" in attrs:
                pads = list(attrs["pads"])
                if len(pads) == 2:
                    pads = [pads[0], pads[1], pads[0], pads[1]]
            elif auto_pad in ("SAME_UPPER", "SAME_LOWER"):
                oh_same = (h + strides[0] - 1) // strides[0]
                ow_same = (w + strides[1] - 1) // strides[1]
                ph_tot = max(0, (oh_same - 1) * strides[0] + dilations[0] * (kshape[0] - 1) + 1 - h)
                pw_tot = max(0, (ow_same - 1) * strides[1] + dilations[1] * (kshape[1] - 1) + 1 - w)
                pads = [ph_tot // 2, pw_tot // 2, ph_tot - ph_tot // 2, pw_tot - pw_tot // 2]
            else:
                pads = [0, 0, 0, 0]
            if op == "Conv":
                oh = (h + pads[0] + pads[2] - dilations[0] * (kshape[0] - 1) - 1) // strides[0] + 1
                ow = (w + pads[1] + pads[3] - dilations[1] * (kshape[1] - 1) - 1) // strides[1] + 1
            else:
                oh = (h - 1) * strides[0] - (pads[0] + pads[2]) + dilations[0] * (kshape[0] - 1) + 1
                ow = (w - 1) * strides[1] - (pads[1] + pads[3]) + dilations[1] * (kshape[1] - 1) + 1
            shape_map[out] = [n_b, out_ch, oh, ow]
        elif op in ("Gemm", "MatMul"):
            w_arr = const_map.get(node.input[1])
            if w_arr is not None and w_arr.ndim == 2:
                trans_b = int(attrs.get("transB", 0)) if op == "Gemm" else 0
                out_ch = int(w_arr.shape[0]) if trans_b else int(w_arr.shape[1])
                shape_map[out] = [1, out_ch]
        elif op in (
            "Relu",
            "Sigmoid",
            "Tanh",
            "Clip",
            "LeakyRelu",
            "Elu",
            "Selu",
            "Softsign",
            "Softplus",
            "Abs",
            "BatchNormalization",
            "Softmax",
            "LayerNormalization",
            "InstanceNormalization",
            "LRN",
            "Identity",
            "Dropout",
            "QuantizeLinear",
            "DequantizeLinear",
            "Sin",
            "Cos",
            "Atan",
            "Exp",
            "Log",
            "Reciprocal",
            "Ceil",
            "Floor",
            "Round",
            "Sqrt",
            "Erf",
            "Neg",
            "Sign",
            "Tan",
            "Sinh",
            "Cosh",
            "Asin",
            "Acos",
        ) and sh0:
            shape_map[out] = list(sh0)
        elif op in (
            "ReduceMean",
            "ReduceSum",
            "ReduceMax",
            "ReduceMin",
            "ReduceL1",
            "ReduceL2",
            "ReduceSumSquare",
        ) and sh0:
            keepdims = int(attrs.get("keepdims", 1))
            if len(node.input) > 1 and node.input[1] in const_map:
                axes = [int(x) for x in const_map[node.input[1]].reshape(-1)]
            else:
                axes = [int(x) for x in attrs.get("axes", [2, 3])]
            norm_axes = {ax + len(sh0) if ax < 0 else ax for ax in axes}
            if keepdims:
                shape_map[out] = [1 if i in norm_axes else d for i, d in enumerate(sh0)]
            else:
                shape_map[out] = [d for i, d in enumerate(sh0) if i not in norm_axes] or [1]
        elif op == "Slice" and sh0 and len(sh0) == 4:
            if len(node.input) >= 3 and node.input[1] in const_map and node.input[2] in const_map:
                raw_starts = [int(x) for x in const_map[node.input[1]].reshape(-1)]
                raw_ends = [int(x) for x in const_map[node.input[2]].reshape(-1)]
                raw_axes = (
                    [int(x) for x in const_map[node.input[3]].reshape(-1)]
                    if len(node.input) > 3 and node.input[3] in const_map
                    else list(range(len(raw_starts)))
                )
                raw_steps = (
                    [int(x) for x in const_map[node.input[4]].reshape(-1)]
                    if len(node.input) > 4 and node.input[4] in const_map
                    else [1] * len(raw_starts)
                )
            else:
                raw_starts = [int(x) for x in attrs.get("starts", [])]
                raw_ends = [int(x) for x in attrs.get("ends", [])]
                raw_axes = [int(x) for x in attrs.get("axes", list(range(len(raw_starts))))]
                raw_steps = [1] * len(raw_starts)
            out_sh = list(sh0)
            for s, e, ax, st in zip(raw_starts, raw_ends, raw_axes, raw_steps):
                if ax < 0:
                    ax += 4
                if 0 <= ax < 4 and st > 0:
                    dim = sh0[ax]
                    if s < 0:
                        s += dim
                    s = max(0, min(s, dim))
                    if e < 0:
                        e += dim
                    e = max(s, min(e, dim))
                    out_sh[ax] = max(1, (e - s + st - 1) // st)
            shape_map[out] = out_sh
        elif op in ("Resize", "Upsample") and sh0 and len(sh0) == 4:
            if op != "Upsample" and len(node.input) > 3 and node.input[3] in const_map:
                sizes_arr = const_map[node.input[3]].reshape(-1)
                if sizes_arr.size == 4:
                    shape_map[out] = [int(x) for x in sizes_arr]
            else:
                sc_arr = None
                if op == "Upsample":
                    if len(node.input) > 1 and node.input[1] in const_map:
                        sc_arr = const_map[node.input[1]].reshape(-1)
                    elif "scales" in attrs:
                        sc_arr = np.asarray(attrs["scales"], dtype=np.float32)
                else:
                    if len(node.input) > 2 and node.input[2] in const_map:
                        sc_arr = const_map[node.input[2]].reshape(-1)
                    elif len(node.input) == 2 and node.input[1] in const_map:
                        sc_arr = const_map[node.input[1]].reshape(-1)
                if sc_arr is not None:
                    if sc_arr.size == 4:
                        shape_map[out] = [
                            max(1, int(round(d * float(s)))) for d, s in zip(sh0, sc_arr)
                        ]
                    elif sc_arr.size == 2:
                        shape_map[out] = [
                            sh0[0],
                            sh0[1],
                            max(1, int(round(sh0[2] * float(sc_arr[0])))),
                            max(1, int(round(sh0[3] * float(sc_arr[1])))),
                        ]
        elif op == "Pad" and sh0 and len(sh0) == 4:
            if len(node.input) > 1 and node.input[1] in const_map:
                raw_pads = [int(x) for x in const_map[node.input[1]].reshape(-1)]
            else:
                raw_pads = [int(x) for x in attrs.get("pads", [])]
            if len(raw_pads) == 8:
                shape_map[out] = [sh0[i] + raw_pads[i] + raw_pads[i + 4] for i in range(4)]
            elif len(raw_pads) == 4:
                shape_map[out] = [
                    sh0[0],
                    sh0[1],
                    sh0[2] + raw_pads[0] + raw_pads[2],
                    sh0[3] + raw_pads[1] + raw_pads[3],
                ]
        elif op in ("Add", "Sub", "Mul", "Div", "Max", "Min"):
            if sh0:
                shape_map[out] = list(sh0)
            elif len(node.input) > 1 and node.input[1] in shape_map:
                shape_map[out] = list(shape_map[node.input[1]])
        elif op in ("MaxPool", "AveragePool", "GlobalAveragePool") and sh0 and len(sh0) == 4:
            n_b, c, h, w = sh0
            if op == "GlobalAveragePool":
                shape_map[out] = [n_b, c, 1, 1]
            else:
                window = list(attrs.get("kernel_shape", [2, 2]))
                strides = list(attrs.get("strides", window))
                pads = list(attrs.get("pads", [0, 0, 0, 0]))
                if len(pads) == 2:
                    pads = [pads[0], pads[1], pads[0], pads[1]]
                oh = (h + pads[0] + pads[2] - window[0]) // strides[0] + 1
                ow = (w + pads[1] + pads[3] - window[1]) // strides[1] + 1
                shape_map[out] = [n_b, c, oh, ow]
        elif op == "Concat":
            axis = int(attrs.get("axis", 1))
            in_shapes = [shape_map.get(x) for x in node.input]
            if all(in_shapes):
                base = list(in_shapes[0])  # type: ignore[arg-type]
                base[axis] = sum(s[axis] for s in in_shapes if s)
                shape_map[out] = base
        elif op == "DepthToSpace" and sh0 and len(sh0) == 4:
            blocksize = int(attrs.get("blocksize", 2))
            n_b, c, h, w = sh0
            shape_map[out] = [n_b, c // (blocksize * blocksize), h * blocksize, w * blocksize]
        elif op == "Flatten" and sh0:
            axis = int(attrs.get("axis", 1))
            d0 = int(np.prod(sh0[:axis])) if axis > 0 else 1
            d1 = int(np.prod(sh0[axis:]))
            shape_map[out] = [d0, d1]
        elif op == "Reshape" and len(node.input) > 1 and node.input[1] in const_map and sh0:
            raw_target = [int(x) for x in const_map[node.input[1]].reshape(-1)]
            total = int(np.prod(sh0))
            resolved = list(raw_target)
            for i, v in enumerate(resolved):
                if v == 0 and i < len(sh0):
                    resolved[i] = sh0[i]
            if -1 in resolved:
                idx = resolved.index(-1)
                known = int(np.prod([x for x in resolved if x != -1]))
                resolved[idx] = total // max(1, known)
            shape_map[out] = resolved

    # Ensure graph.output ValueInfoProto matches inferred shape_map
    for out_vi in graph.output:
        if out_vi.name in shape_map and out_vi.type.HasField("tensor_type"):
            del out_vi.type.tensor_type.shape.dim[:]
            for d_val in shape_map[out_vi.name]:
                dim = out_vi.type.tensor_type.shape.dim.add()
                dim.dim_value = int(d_val)


def is_node_dla_supported(
    node: onnx.NodeProto,
    const_map: Mapping[str, np.ndarray],
    shape_map: Mapping[str, list[int]],
    *,
    fallback_ops: set[str],
    enable_dla_pixel_shuffle: bool,
) -> bool:
    """Return True if `node` can be lowered and executed on the N1X NPU (t254)."""
    op = node.op_type
    if op in fallback_ops:
        return False
    attrs = _extract_attr_dict(node)
    in0 = node.input[0] if node.input else ""
    sh0 = shape_map.get(in0)
    out0 = node.output[0] if node.output else ""
    sh_out = shape_map.get(out0)

    if not sh0 or sh0[0] != 1:
        return False
    if sh_out and sh_out[0] != 1:
        return False

    if op in ("Conv", "ConvTranspose"):
        if len(sh0) != 4 or len(node.input) < 2 or node.input[1] not in const_map:
            return False
        if len(node.input) > 2 and node.input[2] and node.input[2] not in const_map:
            return False
        w_arr = const_map[node.input[1]]
        if w_arr.ndim != 4:
            return False
        kh, kw = int(w_arr.shape[2]), int(w_arr.shape[3])
        strides = list(attrs.get("strides", [1, 1]))
        dilations = list(attrs.get("dilations", [1, 1]))
        return (
            1 <= kh <= 32
            and 1 <= kw <= 32
            and 1 <= strides[0] <= 8
            and 1 <= strides[1] <= 8
            and 1 <= dilations[0] <= 8
            and 1 <= dilations[1] <= 8
        )

    if op in ("Gemm", "MatMul"):
        if len(node.input) < 2 or node.input[1] not in const_map:
            return False
        if len(node.input) > 2 and node.input[2] and node.input[2] not in const_map:
            return False
        w_arr = const_map[node.input[1]]
        if w_arr.ndim != 2:
            return False
        if op == "Gemm":
            if int(attrs.get("transA", 0)) != 0:
                return False
            if float(attrs.get("alpha", 1.0)) != 1.0 or float(attrs.get("beta", 1.0)) not in (0.0, 1.0):
                return False
        return len(sh0) in (2, 4)

    if op in ("Relu", "Sigmoid", "Tanh", "Clip", "LeakyRelu", "Elu", "Selu", "Softsign", "Softplus", "Abs"):
        return len(sh0) in (2, 4)

    if op in ("MaxPool", "AveragePool", "GlobalAveragePool"):
        if len(sh0) != 4 or int(attrs.get("ceil_mode", 0)) != 0:
            return False
        if op == "GlobalAveragePool":
            return 1 <= sh0[2] <= 8 and 1 <= sh0[3] <= 8
        window = list(attrs.get("kernel_shape", [2, 2]))
        strides = list(attrs.get("strides", window))
        return (
            1 <= window[0] <= 8
            and 1 <= window[1] <= 8
            and 1 <= strides[0] <= 8
            and 1 <= strides[1] <= 8
        )

    if op in ("Add", "Sub", "Mul", "Div", "Max", "Min"):
        if len(node.input) != 2:
            return False
        in1 = node.input[1]
        if in0 in const_map and in1 not in const_map and op in ("Add", "Mul"):
            in0, in1 = in1, in0
            sh0 = shape_map.get(in0)
        if not sh0 or len(sh0) not in (2, 4):
            return False
        if in1 in const_map:
            if op not in ("Add", "Sub", "Mul", "Div"):
                return False
            c_arr = const_map[in1]
            if op == "Div" and np.any(c_arr == 0):
                return False
            c_dim = sh0[1]
            return c_arr.size in (1, c_dim)
        if op == "Div":
            return False
        sh1 = shape_map.get(in1)
        return sh1 == sh0 and len(sh0) == 4

    if op == "BatchNormalization":
        return (
            len(sh0) == 4
            and len(node.output) == 1
            and len(node.input) >= 5
            and all(inp in const_map for inp in node.input[1:5])
        )

    if op == "Concat":
        axis = int(attrs.get("axis", 1))
        if axis != 1 or len(sh0) != 4:
            return False
        return all(
            shape_map.get(inp) is not None
            and len(shape_map[inp]) == 4
            and shape_map[inp][0] == sh0[0]
            and shape_map[inp][2:] == sh0[2:]
            for inp in node.input
        )

    if op == "DepthToSpace":
        if not enable_dla_pixel_shuffle or len(sh0) != 4:
            return False
        mode = attrs.get("mode", "DCR")
        blocksize = int(attrs.get("blocksize", 2))
        return mode == "DCR" and blocksize >= 2 and sh0[1] % (blocksize * blocksize) == 0

    if op in ("QuantizeLinear", "DequantizeLinear"):
        return (
            len(sh0) in (2, 4)
            and len(node.input) >= 2
            and node.input[1] in const_map
            and (len(node.input) < 3 or not node.input[2] or node.input[2] in const_map)
        )

    if op in ("Identity", "Dropout"):
        return len(sh0) in (2, 4)

    if op in ("Flatten", "Reshape"):
        # Supported inside a DLA subgraph when flattening [1, C, 1, 1] -> [1, C] or [1, C, H, W] -> [1, C*H*W]
        return (
            sh_out is not None
            and len(sh0) in (2, 4)
            and len(sh_out) in (2, 4)
            and int(np.prod(sh0)) == int(np.prod(sh_out))
        )

    if op == "Slice":
        if len(sh0) != 4:
            return False
        if len(node.input) >= 3:
            if node.input[1] not in const_map or node.input[2] not in const_map:
                return False
            if len(node.input) > 3 and node.input[3] and node.input[3] not in const_map:
                return False
            if len(node.input) > 4 and node.input[4]:
                if node.input[4] not in const_map:
                    return False
                steps = [int(x) for x in const_map[node.input[4]].reshape(-1)]
                if any(st != 1 for st in steps):
                    return False
            return True
        return "starts" in attrs and "ends" in attrs

    if op in ("Resize", "Upsample"):
        if len(sh0) != 4 or not sh_out or len(sh_out) != 4:
            return False
        if sh_out[0] != sh0[0] or sh_out[1] != sh0[1]:
            return False
        if sh_out[2] < sh0[2] or sh_out[3] < sh0[3]:
            return False
        if sh_out[2] % sh0[2] != 0 or sh_out[3] % sh0[3] != 0:
            return False
        rmode = str(attrs.get("mode", "nearest")).lower()
        if rmode == "nearest":
            return True
        if rmode in ("linear", "bilinear"):
            return sh0[1] == 1 and sh_out[2] == 2 * sh0[2] and sh_out[3] == 2 * sh0[3]
        return False

    if op == "Softmax":
        axis = int(attrs.get("axis", 1))
        if len(sh0) == 2:
            return axis in (1, -1)
        if len(sh0) == 4 and sh0[2] == 1 and sh0[3] == 1:
            return axis in (1, -3)
        return False

    if op in (
        "ReduceMean",
        "ReduceSum",
        "ReduceMax",
        "ReduceMin",
        "ReduceL1",
        "ReduceL2",
        "ReduceSumSquare",
    ):
        if len(sh0) != 4 or int(attrs.get("keepdims", 1)) != 1:
            return False
        if len(node.input) > 1 and node.input[1]:
            if node.input[1] not in const_map:
                return False
            axes = [int(x) for x in const_map[node.input[1]].reshape(-1)]
        else:
            axes = [int(x) for x in attrs.get("axes", [2, 3])]
        norm_axes = [ax + 4 if ax < 0 else ax for ax in axes]
        return bool(norm_axes) and all(ax in (1, 2, 3) for ax in norm_axes)

    if op in (
        "Sin",
        "Cos",
        "Atan",
        "Exp",
        "Log",
        "Reciprocal",
        "Ceil",
        "Floor",
        "Round",
        "Sqrt",
        "Erf",
        "Neg",
        "Sign",
        "Tan",
        "Sinh",
        "Cosh",
        "Asin",
        "Acos",
    ):
        return len(sh0) in (2, 4)

    if op in ("LayerNormalization", "InstanceNormalization"):
        if len(sh0) != 4 or len(node.output) != 1:
            return False
        if len(node.input) > 1 and node.input[1] and node.input[1] not in const_map:
            return False
        if len(node.input) > 2 and node.input[2] and node.input[2] not in const_map:
            return False
        return True

    if op == "Pad":
        if len(sh0) != 4:
            return False
        if len(node.input) > 1 and node.input[1]:
            if node.input[1] not in const_map:
                return False
            raw_pads = [int(x) for x in const_map[node.input[1]].reshape(-1)]
        else:
            raw_pads = [int(x) for x in attrs.get("pads", [])]
        if len(node.input) > 2 and node.input[2] and node.input[2] not in const_map:
            return False
        if len(raw_pads) == 8:
            pre_pad, post_pad = raw_pads[:4], raw_pads[4:8]
        elif len(raw_pads) == 4:
            pre_pad, post_pad = [0, 0, raw_pads[0], raw_pads[1]], [0, 0, raw_pads[2], raw_pads[3]]
        else:
            return False
        return (
            pre_pad[0] == 0
            and pre_pad[1] == 0
            and post_pad[0] == 0
            and post_pad[1] == 0
            and all(p >= 0 for p in pre_pad + post_pad)
        )

    if op == "LRN":
        return len(sh0) == 4

    return False


def _build_stage_submodel(
    nodes: Sequence[onnx.NodeProto],
    const_map: Mapping[str, np.ndarray],
    shape_map: Mapping[str, list[int]],
    graph_inputs: set[str],
    needed_after: set[str],
    opset_version: int = 13,
) -> tuple[onnx.ModelProto, list[str], list[str]]:
    produced = {o for n in nodes for o in n.output if o}
    consumed: list[str] = []
    seen_in: set[str] = set()
    for n in nodes:
        for inp in n.input:
            if inp and inp not in const_map and inp not in produced and inp not in seen_in:
                seen_in.add(inp)
                consumed.append(inp)

    outputs: list[str] = []
    seen_out: set[str] = set()
    for n in nodes:
        for out in n.output:
            if out and out in needed_after and out not in seen_out:
                seen_out.add(out)
                outputs.append(out)

    in_vis = [
        helper.make_tensor_value_info(name, TensorProto.FLOAT, shape_map.get(name, [1, 1, 1, 1]))
        for name in consumed
    ]
    out_vis = [
        helper.make_tensor_value_info(name, TensorProto.FLOAT, shape_map.get(name, [1, 1, 1, 1]))
        for name in outputs
    ]
    used_consts = {inp for n in nodes for inp in n.input if inp in const_map}
    inits = [numpy_helper.from_array(const_map[name], name=name) for name in sorted(used_consts)]

    sub_graph = helper.make_graph(
        list(nodes),
        name="n1x_stage",
        inputs=in_vis,
        outputs=out_vis,
        initializer=inits,
    )
    opset = helper.make_opsetid("", opset_version)
    sub_model = helper.make_model(sub_graph, opset_imports=[opset])
    sub_model.ir_version = 8
    return sub_model, consumed, outputs


def calibrate_onnx_model(
    model_or_path: Any,
    calibration_data: Iterable[Mapping[str, np.ndarray]] | None = None,
    input_shapes: Mapping[str, Sequence[int]] | None = None,
    num_synthetic_samples: int = 4,
    *,
    per_channel: bool = False,
) -> dict[str, Any]:
    """Run calibration samples through `model_or_path` and collect per-tensor and per-channel max_abs ranges."""
    canon_model, shape_map = canonicalize_onnx_model(model_or_path, input_shapes=input_shapes)
    inst_model = copy.deepcopy(canon_model)
    rewritten_nodes: list[onnx.NodeProto] = []
    for idx, n in enumerate(inst_model.graph.node):
        if n.op_type == "Gemm" and n.input:
            in_a = n.input[0]
            if len(shape_map.get(in_a, ())) > 2:
                flat_out = f"{in_a}__calib_flat_{idx}"
                rewritten_nodes.append(
                    helper.make_node("Flatten", inputs=[in_a], outputs=[flat_out], axis=1)
                )
                n_copy = copy.deepcopy(n)
                n_copy.input[0] = flat_out
                rewritten_nodes.append(n_copy)
                continue
        rewritten_nodes.append(n)
    del inst_model.graph.node[:]
    inst_model.graph.node.extend(rewritten_nodes)

    existing_outs = {o.name for o in inst_model.graph.output}
    for n in inst_model.graph.node:
        for out in n.output:
            if out and "__calib_flat_" not in out and out not in existing_outs:
                inst_model.graph.output.append(
                    helper.make_tensor_value_info(out, TensorProto.FLOAT, shape_map.get(out))
                )
                existing_outs.add(out)

    evaluator = ReferenceEvaluator(inst_model)
    samples = list(calibration_data) if calibration_data is not None else []
    if not samples:
        rng = np.random.default_rng(0)
        init_names = {init.name for init in canon_model.graph.initializer}
        for _ in range(max(1, num_synthetic_samples)):
            feed: dict[str, np.ndarray] = {}
            for inp in canon_model.graph.input:
                if inp.name in init_names:
                    continue
                sh = shape_map.get(inp.name, [1, 1, 28, 28])
                feed[inp.name] = rng.uniform(0.0, 1.0, size=sh).astype(np.float32)
            samples.append(feed)

    max_abs_map: dict[str, float] = {}
    channel_max_abs_map: dict[str, np.ndarray] = {}

    def _accumulate_stats(tname: str, arr_np: np.ndarray) -> None:
        val = float(np.max(np.abs(arr_np))) if arr_np.size else 1.0
        max_abs_map[tname] = max(max_abs_map.get(tname, 1e-5), val, 1e-5)
        if arr_np.ndim >= 2 and arr_np.shape[1] >= 1 and arr_np.size > 0:
            reduce_axes = tuple(i for i in range(arr_np.ndim) if i != 1)
            ch_max = np.maximum(np.max(np.abs(arr_np), axis=reduce_axes).astype(np.float64), 1e-5)
            prev_ch = channel_max_abs_map.get(tname)
            if prev_ch is None or prev_ch.shape != ch_max.shape:
                channel_max_abs_map[tname] = ch_max
            else:
                channel_max_abs_map[tname] = np.maximum(prev_ch, ch_max)

    out_names = [o.name for o in inst_model.graph.output]
    for feed in samples:
        np_feed = {k: np.asarray(v, dtype=np.float32) for k, v in feed.items()}
        for k, arr in np_feed.items():
            _accumulate_stats(k, arr)
        results = evaluator.run(None, np_feed)
        for name, arr in zip(out_names, results):
            _accumulate_stats(name, np.asarray(arr, dtype=np.float32))

    calib_dict: dict[str, Any] = {}
    for name, val in max_abs_map.items():
        entry: dict[str, Any] = {"dynamic_range": [-val, val]}
        if per_channel and name in channel_max_abs_map:
            entry["channel_dynamic_ranges"] = [
                [-float(c_val), float(c_val)] for c_val in channel_max_abs_map[name]
            ]
        calib_dict[name] = entry
    calib_dict["tensor_ranges"] = max_abs_map
    calib_dict["channel_ranges"] = {
        name: [float(c_val) for c_val in ch_arr]
        for name, ch_arr in channel_max_abs_map.items()
    }
    return calib_dict


@dataclass
class ExecutionStage:
    stage_id: int
    kind: str  # "DLA" or "Host"
    nodes: list[onnx.NodeProto]
    inputs: list[str]
    outputs: list[str]
    input_shapes: list[list[int]]
    output_shapes: list[list[int]]
    submodel: onnx.ModelProto
    precision: str = "fp16"
    nvda_bytes: bytes | None = None
    cache_hit: bool = False
    cache_path: Path | None = None
    dla_module: N1xOrtModuleWrapper | None = None
    host_runner: Any = None
    tensor_ranges: dict[str, float] = field(default_factory=dict)


def _compute_dag_cache_key(dag_spec: dict[str, Any]) -> str:
    canonical_json = json.dumps(dag_spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_json).hexdigest()


def _compile_or_load_cached_dag(
    dag_spec: dict[str, Any],
    cache_dir: Path | None,
) -> tuple[bytes, bool, Path | None]:
    if cache_dir is not None:
        digest = _compute_dag_cache_key(dag_spec)
        cache_file = cache_dir / f"{digest}.nvda"
        if cache_file.is_file():
            return cache_file.read_bytes(), True, cache_file
        blob = compile_dag(dag_spec)
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = cache_dir / f".{digest}.{os.getpid()}.tmp"
        tmp_file.write_bytes(blob)
        tmp_file.replace(cache_file)
        return blob, False, cache_file
    return compile_dag(dag_spec), False, None


def partition_onnx_model(
    model_or_path: Any,
    *,
    precision: str = "fp16",
    calibration: dict[str, Any] | None = None,
    calibration_data: Iterable[Mapping[str, np.ndarray]] | None = None,
    input_shapes: Mapping[str, Sequence[int]] | None = None,
    cache_dir: Path | str | None = DEFAULT_CACHE_DIR,
    min_dla_ops: int = 1,
    fallback_ops: Iterable[str] = (),
    enable_dla_pixel_shuffle: bool = False,
) -> tuple[onnx.ModelProto, list[ExecutionStage], dict[str, list[int]]]:
    """Canonicalize and partition an ONNX model into cycle-free DLA and Host stages."""
    canon_model, shape_map = canonicalize_onnx_model(model_or_path, input_shapes=input_shapes)
    graph = canon_model.graph
    const_map = {init.name: numpy_helper.to_array(init) for init in graph.initializer}
    opset_version = int(canon_model.opset_import[0].version) if canon_model.opset_import else 13
    fallback_set = set(fallback_ops)

    has_explicit_qdq = any(
        n.op_type in ("QuantizeLinear", "DequantizeLinear") for n in graph.node
    )
    resolved_calib = calibration
    if precision == "int8" and not has_explicit_qdq and resolved_calib is None:
        resolved_calib = calibrate_onnx_model(
            canon_model, calibration_data=calibration_data, input_shapes=input_shapes
        )
    tensor_ranges: dict[str, float] = {}
    if resolved_calib:
        raw_ranges = resolved_calib.get("tensor_ranges", resolved_calib)
        tensor_ranges = {str(k): float(v) for k, v in raw_ranges.items() if isinstance(v, (int, float))}

    # Group contiguous nodes along topological order into raw (kind, nodes) runs
    raw_runs: list[tuple[str, list[onnx.NodeProto]]] = []
    for node in graph.node:
        supported = is_node_dla_supported(
            node,
            const_map,
            shape_map,
            fallback_ops=fallback_set,
            enable_dla_pixel_shuffle=enable_dla_pixel_shuffle,
        )
        kind = "DLA" if supported else "Host"
        if raw_runs and raw_runs[-1][0] == kind:
            raw_runs[-1][1].append(node)
        else:
            raw_runs.append((kind, [node]))

    # Demote DLA runs that only contain alias/reshape ops or fewer than min_dla_ops compute ops
    alias_ops = {"Identity", "Dropout", "Flatten", "Reshape", "Squeeze", "Unsqueeze"}
    coarsened_runs: list[tuple[str, list[onnx.NodeProto]]] = []
    for kind, run_nodes in raw_runs:
        if kind == "DLA":
            compute_count = sum(1 for n in run_nodes if n.op_type not in alias_ops)
            if compute_count < max(1, min_dla_ops):
                kind = "Host"
        if coarsened_runs and coarsened_runs[-1][0] == kind:
            coarsened_runs[-1][1].extend(run_nodes)
        else:
            coarsened_runs.append((kind, list(run_nodes)))

    # Resolve cache_dir
    resolved_cache_dir: Path | None = None
    if cache_dir is not None and str(cache_dir) != "":
        resolved_cache_dir = Path(cache_dir)

    graph_input_names = {inp.name for inp in graph.input if inp.name not in const_map}
    graph_output_names = {out.name for out in graph.output}

    # Build ExecutionStages with compiler-verified fallback if libnvdla_compiler rejects a tail op
    stages: list[ExecutionStage] = []
    idx = 0
    while idx < len(coarsened_runs):
        kind, run_nodes = coarsened_runs[idx]
        # Compute tensors needed by all subsequent runs + graph.output
        needed_after = set(graph_output_names)
        for future_kind, future_nodes in coarsened_runs[idx + 1 :]:
            for fn in future_nodes:
                for inp in fn.input:
                    if inp and inp not in const_map:
                        needed_after.add(inp)

        if kind == "DLA":
            # Strip trailing Flatten/Reshape if its output is only needed by Host or graph.output
            # while its input is already 4D (unless followed inside the same stage by Gemm)
            submodel, stage_ins, stage_outs = _build_stage_submodel(
                run_nodes, const_map, shape_map, graph_input_names, needed_after, opset_version
            )
            try:
                in_sh_override = {
                    name: shape_map[name] for name in stage_ins if name in shape_map
                }
                dag_spec = onnx_to_dag(
                    submodel,
                    compute_precision=precision,
                    calibration={"tensor_ranges": tensor_ranges} if tensor_ranges else None,
                    explicit_qdq=has_explicit_qdq,
                    input_shapes=in_sh_override,
                    include_pixel_shuffle=enable_dla_pixel_shuffle,
                )
                nvda_blob, hit, cpath = _compile_or_load_cached_dag(dag_spec, resolved_cache_dir)
                stages.append(
                    ExecutionStage(
                        stage_id=len(stages),
                        kind="DLA",
                        nodes=run_nodes,
                        inputs=stage_ins,
                        outputs=stage_outs,
                        input_shapes=[shape_map.get(x, [1, 1, 1, 1]) for x in stage_ins],
                        output_shapes=[shape_map.get(x, [1, 1, 1, 1]) for x in stage_outs],
                        submodel=submodel,
                        precision=precision,
                        nvda_bytes=nvda_blob,
                        cache_hit=hit,
                        cache_path=cpath,
                        tensor_ranges=tensor_ranges,
                    )
                )
                idx += 1
                continue
            except CompilerError:
                # Compiler-verified fallback: peel off the last node to Host and retry if >1 nodes
                if len(run_nodes) > 1:
                    tail_node = run_nodes.pop()
                    if idx + 1 < len(coarsened_runs) and coarsened_runs[idx + 1][0] == "Host":
                        coarsened_runs[idx + 1][1].insert(0, tail_node)
                    else:
                        coarsened_runs.insert(idx + 1, ("Host", [tail_node]))
                    continue
                # Single-node DLA stage failed compilation; demote to Host
                kind = "Host"

        submodel, stage_ins, stage_outs = _build_stage_submodel(
            run_nodes, const_map, shape_map, graph_input_names, needed_after, opset_version
        )
        stages.append(
            ExecutionStage(
                stage_id=len(stages),
                kind="Host",
                nodes=run_nodes,
                inputs=stage_ins,
                outputs=stage_outs,
                input_shapes=[shape_map.get(x, [1, 1, 1, 1]) for x in stage_ins],
                output_shapes=[shape_map.get(x, [1, 1, 1, 1]) for x in stage_outs],
                submodel=submodel,
                precision="fp32",
            )
        )
        idx += 1

    return canon_model, stages, shape_map


@dataclass(frozen=True)
class N1xNodeArg:
    """`onnxruntime.NodeArg`-compatible input/output metadata."""

    name: str
    shape: list[int]
    type: str = "tensor(float)"


class _HostStageRunner:
    """Executes a Host ONNX subgraph via `onnxruntime` (if installed) or `ReferenceEvaluator`."""

    def __init__(self, submodel: onnx.ModelProto) -> None:
        self._outputs = [o.name for o in submodel.graph.output]
        self._ort_sess: Any = None
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]

            self._ort_sess = ort.InferenceSession(
                submodel.SerializeToString(), providers=["CPUExecutionProvider"]
            )
            self._eval = None
        except Exception:
            self._eval = ReferenceEvaluator(submodel)

    def run(self, feed: Mapping[str, np.ndarray]) -> list[np.ndarray]:
        if self._ort_sess is not None:
            return [np.asarray(x, dtype=np.float32) for x in self._ort_sess.run(self._outputs, dict(feed))]
        return [np.asarray(x, dtype=np.float32) for x in self._eval.run(self._outputs, dict(feed))]


class N1xInferenceSession:
    """`onnxruntime.InferenceSession`-compatible heterogeneous execution session for N1X NPU."""

    def __init__(
        self,
        path_or_bytes: Any,
        sess_options: Any = None,
        providers: Sequence[str | tuple[str, Mapping[str, Any]]] | None = None,
        provider_options: Sequence[Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        del sess_options
        ep_opts: dict[str, Any] = dict(kwargs)
        provider_names: list[str] = []
        if providers:
            for idx, p in enumerate(providers):
                if isinstance(p, tuple):
                    pname, popts = p
                    provider_names.append(str(pname))
                    if pname == "N1xExecutionProvider" and popts:
                        ep_opts.update(popts)
                else:
                    provider_names.append(str(p))
                    if (
                        p == "N1xExecutionProvider"
                        and provider_options
                        and idx < len(provider_options)
                        and provider_options[idx]
                    ):
                        ep_opts.update(provider_options[idx])
        if not provider_names:
            provider_names = ["N1xExecutionProvider", "CPUExecutionProvider"]
        self._providers = provider_names

        precision = str(ep_opts.get("precision", "fp16")).lower()
        calibration = ep_opts.get("calibration")
        calibration_data = ep_opts.get("calibration_data")
        input_shapes = ep_opts.get("input_shapes")
        cache_dir = ep_opts.get("cache_dir", DEFAULT_CACHE_DIR)
        min_dla_ops = int(ep_opts.get("min_dla_ops", 1))
        fallback_ops = ep_opts.get("fallback_ops", ())
        enable_dla_pixel_shuffle = bool(ep_opts.get("enable_dla_pixel_shuffle", False))
        device_path = ep_opts.get("device_path")
        accel_fd = int(ep_opts.get("accel_fd", -1))

        self._model, self._stages, self._shape_map = partition_onnx_model(
            path_or_bytes,
            precision=precision,
            calibration=calibration,
            calibration_data=calibration_data,
            input_shapes=input_shapes,
            cache_dir=cache_dir,
            min_dla_ops=min_dla_ops,
            fallback_ops=fallback_ops,
            enable_dla_pixel_shuffle=enable_dla_pixel_shuffle,
        )

        init_names = {init.name for init in self._model.graph.initializer}
        self._inputs = [
            N1xNodeArg(
                name=inp.name,
                shape=list(self._shape_map.get(inp.name, [1, 1, 1, 1])),
            )
            for inp in self._model.graph.input
            if inp.name not in init_names
        ]
        self._outputs = [
            N1xNodeArg(
                name=out.name,
                shape=list(self._shape_map.get(out.name, [1, 1, 1, 1])),
            )
            for out in self._model.graph.output
        ]

        # Initialize runtime wrappers for each stage
        for st in self._stages:
            if st.kind == "DLA":
                assert st.nvda_bytes is not None
                mod = N1xOrtModuleWrapper(
                    st.nvda_bytes,
                    accel_fd=accel_fd,
                    device_path=device_path,
                )
                if st.precision == "int8":
                    for i, in_name in enumerate(st.inputs):
                        max_abs = st.tensor_ranges.get(in_name, 127.0)
                        mod.set_input_quant(i, max(max_abs, 1e-5) / 127.0, 0)
                    for i, out_name in enumerate(st.outputs):
                        max_abs = st.tensor_ranges.get(out_name, 127.0)
                        mod.set_output_quant(i, max(max_abs, 1e-5) / 127.0, 0)
                st.dla_module = mod
            else:
                st.host_runner = _HostStageRunner(st.submodel)

    def close(self) -> None:
        for st in getattr(self, "_stages", ()):
            if st.dla_module is not None:
                st.dla_module.close()

    def __enter__(self) -> "N1xInferenceSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def get_inputs(self) -> list[N1xNodeArg]:
        return list(self._inputs)

    def get_outputs(self) -> list[N1xNodeArg]:
        return list(self._outputs)

    def get_providers(self) -> list[str]:
        return list(self._providers)

    def get_partition_summary(self) -> dict[str, Any]:
        dla_stages = sum(1 for s in self._stages if s.kind == "DLA")
        host_stages = sum(1 for s in self._stages if s.kind == "Host")
        return {
            "num_stages": len(self._stages),
            "dla_stages": dla_stages,
            "host_stages": host_stages,
            "stages": [
                {
                    "stage_id": s.stage_id,
                    "kind": s.kind,
                    "ops": [(n.op_type, n.name or "") for n in s.nodes],
                    "inputs": list(s.inputs),
                    "outputs": list(s.outputs),
                    "precision": s.precision,
                    "nvda_bytes": len(s.nvda_bytes) if s.nvda_bytes else 0,
                    "cache_hit": s.cache_hit,
                    "run_count": s.dla_module.run_count if s.dla_module else 0,
                    "used_resubmit": s.dla_module.used_resubmit if s.dla_module else False,
                }
                for s in self._stages
            ],
        }

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, np.ndarray],
        run_options: Any = None,
    ) -> list[np.ndarray]:
        del run_options
        env: dict[str, np.ndarray] = {
            k: np.ascontiguousarray(v, dtype=np.float32) for k, v in input_feed.items()
        }
        for st in self._stages:
            if st.kind == "DLA":
                assert st.dla_module is not None
                stage_in_arrays: list[np.ndarray] = []
                for idx, in_name in enumerate(st.inputs):
                    arr = env[in_name]
                    desc_shape = st.dla_module.inputs[idx].shape
                    if arr.shape != desc_shape:
                        arr = arr.reshape(desc_shape)
                    stage_in_arrays.append(arr)
                raw_outs = st.dla_module.run_fp32(stage_in_arrays)
                for out_name, out_sh, raw_arr in zip(st.outputs, st.output_shapes, raw_outs):
                    env[out_name] = raw_arr.reshape(out_sh)
            else:
                assert st.host_runner is not None
                sub_feed = {
                    in_name: env[in_name].reshape(in_sh)
                    for in_name, in_sh in zip(st.inputs, st.input_shapes)
                }
                host_outs = st.host_runner.run(sub_feed)
                for out_name, out_sh, arr in zip(st.outputs, st.output_shapes, host_outs):
                    env[out_name] = arr.reshape(out_sh)

        target_outs = (
            list(output_names)
            if output_names is not None
            else [o.name for o in self._outputs]
        )
        return [env[name] for name in target_outs]


InferenceSession = N1xInferenceSession
