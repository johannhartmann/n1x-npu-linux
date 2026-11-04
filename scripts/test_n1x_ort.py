#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline unit and integration tests for N1X C-Backed Heterogeneous ONNX Execution Provider."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

from n1x_dla import (
    dequantize_int8,
    pack_chw16_fp16,
    pack_chw32_int8,
    quantize_int8,
    unpack_chw16_fp16,
    unpack_chw32_int8,
)
from n1x_ort import (
    InferenceSession,
    N1xInferenceSession,
    TensorDesc,
    calibrate_onnx_model,
    canonicalize_onnx_model,
    get_ort_client,
)

MNIST_ONNX = ROOT / "sources" / "demo-research" / "mnist-12.onnx"
SUPERRES_ONNX = ROOT / "sources" / "demo-research" / "super-resolution-10.onnx"
EMOTION_ONNX = ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx"
ORT_RUN_BIN = ROOT / "runtime" / "n1x-ort-run"


def _build_three_stage_onnx() -> onnx.ModelProto:
    """Build Conv -> Relu -> Softmax (Host) -> Conv+Add -> Relu ONNX graph."""
    rng = np.random.default_rng(42)
    w1 = rng.normal(0.0, 0.2, size=(16, 16, 3, 3)).astype(np.float32)
    b1 = rng.uniform(-0.1, 0.1, size=(16,)).astype(np.float32)
    w2 = rng.normal(0.0, 0.2, size=(16, 16, 1, 1)).astype(np.float32)
    b2 = rng.uniform(-0.1, 0.1, size=(1, 16, 1, 1)).astype(np.float32)

    nodes = [
        helper.make_node(
            "Conv",
            inputs=["input", "w1", "b1"],
            outputs=["c1"],
            name="conv1",
            kernel_shape=[3, 3],
            pads=[1, 1, 1, 1],
        ),
        helper.make_node("Relu", inputs=["c1"], outputs=["r1"], name="relu1"),
        helper.make_node("Softmax", inputs=["r1"], outputs=["sm"], name="softmax_mid", axis=1),
        helper.make_node(
            "Conv",
            inputs=["sm", "w2"],
            outputs=["c2_unbiased"],
            name="conv2",
            kernel_shape=[1, 1],
        ),
        helper.make_node("Add", inputs=["c2_unbiased", "b2"], outputs=["c2"], name="conv2_bias_add"),
        helper.make_node("Relu", inputs=["c2"], outputs=["output"], name="relu2"),
    ]
    graph = helper.make_graph(
        nodes,
        "three_stage_net",
        inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 16, 8, 8])],
        outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 16, 8, 8])],
        initializer=[
            numpy_helper.from_array(w1, name="w1"),
            numpy_helper.from_array(b1, name="b1"),
            numpy_helper.from_array(w2, name="w2"),
            numpy_helper.from_array(b2, name="b2"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    return model


class TestN1xOrt(unittest.TestCase):
    def test_surface_pack_unpack_roundtrip(self) -> None:
        client = get_ort_client()
        rng = np.random.default_rng(7)

        # 1. FP16 CHW16 with non-multiple-of-16 channels (C=9) and padded line stride
        shape_fp16 = (1, 9, 5, 6)
        line_fp16 = 256  # >= 6 * 32 = 192
        surf_fp16 = 5 * line_fp16
        desc_fp16 = TensorDesc(
            name="in_fp16",
            size=surf_fp16,
            shape=shape_fp16,
            data_format=3,
            data_type=2,
            data_category=0,
            pixel_format=0,
            pixel_mapping=0,
            strides=(2, line_fp16, surf_fp16, 0, 0, 0, 0, 0),
        )
        arr_fp32 = rng.normal(0.0, 1.0, size=shape_fp16).astype(np.float32)
        c_packed = client.pack_fp32(desc_fp16, arr_fp32)
        py_packed = pack_chw16_fp16(
            arr_fp32, stride_w=line_fp16, stride_h=surf_fp16, total_bytes=surf_fp16
        )
        self.assertEqual(c_packed, py_packed)

        c_unpacked = client.unpack_fp32(desc_fp16, c_packed)
        py_unpacked = unpack_chw16_fp16(
            py_packed, shape_fp16, stride_w=line_fp16, stride_h=surf_fp16
        )
        np.testing.assert_array_equal(c_unpacked, py_unpacked)

        # 2. INT8 CHW32 with per-tensor and per-channel scales
        shape_int8 = (1, 40, 4, 5)
        line_int8 = 256  # >= 5 * 32 = 160
        surf_int8 = 4 * line_int8
        total_int8 = 2 * surf_int8
        desc_int8 = TensorDesc(
            name="in_int8",
            size=total_int8,
            shape=shape_int8,
            data_format=5,
            data_type=4,
            data_category=0,
            pixel_format=36,
            pixel_mapping=0,
            strides=(1, line_int8, surf_int8, 0, 0, 0, 0, 0),
        )
        arr_int8_src = rng.uniform(-2.0, 2.0, size=shape_int8).astype(np.float32)
        per_ch_scale = np.linspace(0.01, 0.08, shape_int8[1], dtype=np.float32)

        c_packed_i8 = client.pack_fp32(desc_int8, arr_int8_src, scale=per_ch_scale, zero_point=0)
        py_q = quantize_int8(arr_int8_src, scale=per_ch_scale, zero_point=0)
        py_packed_i8 = pack_chw32_int8(
            py_q, stride_w=line_int8, stride_h=surf_int8, total_bytes=total_int8
        )
        self.assertEqual(c_packed_i8, py_packed_i8)

        c_unpacked_i8 = client.unpack_fp32(
            desc_int8, c_packed_i8, scale=per_ch_scale, zero_point=0
        )
        py_unpacked_i8 = dequantize_int8(
            unpack_chw32_int8(py_packed_i8, shape_int8, stride_w=line_int8, stride_h=surf_int8),
            scale=per_ch_scale,
            zero_point=0,
        )
        np.testing.assert_allclose(c_unpacked_i8, py_unpacked_i8, rtol=1e-6, atol=1e-6)

        # 3. Reject NaN / Inf inputs
        bad = arr_fp32.copy()
        bad[0, 0, 0, 0] = np.nan
        with self.assertRaises(ValueError):
            client.pack_fp32(desc_fp16, bad)
        bad[0, 0, 0, 0] = np.inf
        with self.assertRaises(ValueError):
            client.pack_fp32(desc_fp16, bad)

    def test_mnist12_canonicalization_and_partition(self) -> None:
        canon, shape_map = canonicalize_onnx_model(MNIST_ONNX)
        ops = [n.op_type for n in canon.graph.node]
        # Conv+Add fused to Conv, Reshape+MatMul+Add fused to Gemm:
        # Conv -> Relu -> MaxPool -> Conv -> Relu -> MaxPool -> Gemm
        self.assertEqual(
            ops,
            ["Conv", "Relu", "MaxPool", "Conv", "Relu", "MaxPool", "Gemm"],
        )
        self.assertEqual(shape_map["Plus214_Output_0"], [1, 10])

        with tempfile.TemporaryDirectory() as tmpdir:
            sess = N1xInferenceSession(
                MNIST_ONNX,
                providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
            )
            summary = sess.get_partition_summary()
            self.assertEqual(summary["num_stages"], 1)
            self.assertEqual(summary["dla_stages"], 1)
            self.assertEqual(summary["host_stages"], 0)
            self.assertFalse(summary["stages"][0]["cache_hit"])
            self.assertGreater(summary["stages"][0]["nvda_bytes"], 2_000_000)

            # Second session hits SHA-256 disk cache
            sess2 = InferenceSession(
                MNIST_ONNX,
                providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
            )
            self.assertTrue(sess2.get_partition_summary()["stages"][0]["cache_hit"])

            # Verify CLI inspector on the cached .nvda file
            nvda_files = list(Path(tmpdir).glob("*.nvda"))
            self.assertEqual(len(nvda_files), 1)
            proc = subprocess.run(
                [str(ORT_RUN_BIN), "--inspect", str(nvda_files[0])],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("shape=[1,1,28,28]", proc.stdout)
            self.assertIn("shape=[1,10,1,1]", proc.stdout)

    def test_superres10_heterogeneous_partition(self) -> None:
        canon, shape_map = canonicalize_onnx_model(
            SUPERRES_ONNX, input_shapes={"input": [1, 1, 64, 64]}
        )
        ops = [n.op_type for n in canon.graph.node]
        # 4 Conv + 3 Relu + fused DepthToSpace
        self.assertEqual(
            ops,
            ["Conv", "Relu", "Conv", "Relu", "Conv", "Relu", "Conv", "DepthToSpace"],
        )
        self.assertEqual(shape_map["output"], [1, 1, 192, 192])

        with tempfile.TemporaryDirectory() as tmpdir:
            sess = N1xInferenceSession(
                SUPERRES_ONNX,
                providers=[
                    (
                        "N1xExecutionProvider",
                        {"input_shapes": {"input": [1, 1, 64, 64]}, "cache_dir": tmpdir},
                    ),
                    "CPUExecutionProvider",
                ],
            )
            summary = sess.get_partition_summary()
            self.assertEqual(summary["num_stages"], 2)
            self.assertEqual(summary["dla_stages"], 1)
            self.assertEqual(summary["host_stages"], 1)
            self.assertEqual(summary["stages"][0]["kind"], "DLA")
            self.assertEqual(summary["stages"][1]["kind"], "Host")
            self.assertEqual([op for op, _ in summary["stages"][1]["ops"]], ["DepthToSpace"])

    def test_emotion_ferplus8_single_stage_partition(self) -> None:
        if not EMOTION_ONNX.is_file():
            self.skipTest(f"{EMOTION_ONNX} not present")
        canon, shape_map = canonicalize_onnx_model(EMOTION_ONNX)
        self.assertEqual(shape_map["Plus692_Output_0"], [1, 8])
        with tempfile.TemporaryDirectory() as tmpdir:
            sess = N1xInferenceSession(
                EMOTION_ONNX,
                providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
            )
            summary = sess.get_partition_summary()
            self.assertEqual(summary["num_stages"], 1)
            self.assertEqual(summary["dla_stages"], 1)
            self.assertEqual(summary["host_stages"], 0)
            self.assertGreater(summary["stages"][0]["nvda_bytes"], 20_000_000)

    def test_multi_stage_partition_and_int8_auto_calibration(self) -> None:
        model = _build_three_stage_onnx()
        rng = np.random.default_rng(99)
        calib_batches = [
            {"input": rng.uniform(0.0, 1.0, size=(1, 16, 8, 8)).astype(np.float32)}
            for _ in range(3)
        ]
        calib_res = calibrate_onnx_model(model, calibration_data=calib_batches)
        self.assertIn("tensor_ranges", calib_res)
        self.assertIn("input", calib_res["tensor_ranges"])
        self.assertIn("output", calib_res["tensor_ranges"])

        with tempfile.TemporaryDirectory() as tmpdir:
            sess = N1xInferenceSession(
                model,
                providers=[
                    (
                        "N1xExecutionProvider",
                        {
                            "precision": "int8",
                            "calibration_data": calib_batches,
                            "cache_dir": tmpdir,
                        },
                    ),
                    "CPUExecutionProvider",
                ],
            )
            summary = sess.get_partition_summary()
            self.assertEqual(summary["num_stages"], 3)
            self.assertEqual(summary["dla_stages"], 2)
            self.assertEqual(summary["host_stages"], 1)
            self.assertEqual(
                [s["kind"] for s in summary["stages"]], ["DLA", "Host", "DLA"]
            )
            # Both DLA stages compiled as INT8
            for stage_idx in (0, 2):
                st = sess._stages[stage_idx]
                assert st.dla_module is not None
                self.assertEqual(st.dla_module.inputs[0].data_type, 4)
                self.assertEqual(st.dla_module.outputs[0].data_type, 4)

    def test_extended_onnx_operators_and_per_channel_calibration(self) -> None:
        scale = np.ones((16,), dtype=np.float32)
        bias = np.zeros((16,), dtype=np.float32)
        pads = np.array([0, 0, 1, 1, 0, 0, 1, 1], dtype=np.int64)
        starts = np.array([1, 1], dtype=np.int64)
        ends = np.array([9, 9], dtype=np.int64)
        axes = np.array([2, 3], dtype=np.int64)
        steps = np.array([1, 1], dtype=np.int64)
        scales_up = np.array([1.0, 1.0, 2.0, 2.0], dtype=np.float32)

        nodes = [
            helper.make_node("Pad", inputs=["input", "pads"], outputs=["padded"], name="pad1", mode="constant"),
            helper.make_node("Slice", inputs=["padded", "starts", "ends", "axes", "steps"], outputs=["sliced"], name="slice1"),
            helper.make_node("InstanceNormalization", inputs=["sliced", "scale", "bias"], outputs=["normed"], name="in1", epsilon=1e-5),
            helper.make_node("Abs", inputs=["normed"], outputs=["u_abs"], name="abs1"),
            helper.make_node("Resize", inputs=["u_abs", "", "scales_up"], outputs=["up"], name="resize1", mode="nearest"),
            helper.make_node("ReduceMean", inputs=["up"], outputs=["pooled"], name="red1", axes=[2, 3], keepdims=1),
            helper.make_node("Softmax", inputs=["pooled"], outputs=["output"], name="sm1", axis=1),
        ]
        graph = helper.make_graph(
            nodes,
            "extended_dla_net",
            inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 16, 8, 8])],
            outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 16, 1, 1])],
            initializer=[
                numpy_helper.from_array(scale, name="scale"),
                numpy_helper.from_array(bias, name="bias"),
                numpy_helper.from_array(pads, name="pads"),
                numpy_helper.from_array(starts, name="starts"),
                numpy_helper.from_array(ends, name="ends"),
                numpy_helper.from_array(axes, name="axes"),
                numpy_helper.from_array(steps, name="steps"),
                numpy_helper.from_array(scales_up, name="scales_up"),
            ],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8

        rng = np.random.default_rng(19)
        calib_feeds = [{"input": rng.normal(0.0, 0.5, size=(1, 16, 8, 8)).astype(np.float32)} for _ in range(2)]
        calib = calibrate_onnx_model(model, calib_feeds, per_channel=True)
        self.assertIn("input", calib["tensor_ranges"])
        self.assertIn("input", calib["channel_ranges"])
        self.assertEqual(len(calib["channel_ranges"]["input"]), 16)
        self.assertIn("channel_dynamic_ranges", calib["input"])

        with tempfile.TemporaryDirectory(prefix="n1x-ort-ext-") as tmp:
            sess = N1xInferenceSession(
                model,
                providers=[("N1xExecutionProvider", {"cache_dir": tmp})],
            )
            summary = sess.get_partition_summary()
            self.assertEqual(summary["num_stages"], 1)
            self.assertEqual(summary["dla_stages"], 1)
            self.assertEqual(summary["host_stages"], 0)

    def test_end_to_end_session_run_and_resubmit_transport(self) -> None:
        mock_dla_c = r"""
#include "n1x_dla.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static unsigned long long next_job = 10;
static void log_event(const char *ev) {
    const char *p = getenv("TEST_ORT_EVENTS");
    if (!p) return;
    FILE *f = fopen(p, "a");
    assert(f);
    fprintf(f, "%s\n", ev);
    fclose(f);
}
int n1x_dla_open(const char *p) {
    (void)p;
    log_event("OPEN");
    if (const char *mfile = getenv("TEST_ORT_MMAP_FILE")) {
        return open(mfile, O_RDWR);
    }
    return open("/dev/null", O_RDWR);
}
int n1x_dla_info(int fd, struct n1x_dla_accel_info *i) {
    assert(fd >= 0);
    memset(i, 0, sizeof(*i));
    i->abi_version = N1X_DLA_ABI_VERSION;
    i->capabilities = N1X_DLA_CAP_REEXEC_IO;
    i->retain_limit = 64U << 20;
    i->max_jobs = 128;
    return 0;
}
int n1x_dla_submit(int fd, const void *image, unsigned int bytes,
                   const struct n1x_dla_accel_range *ranges, unsigned int count,
                   const struct n1x_dla_accel_range *writable, unsigned int writable_count,
                   struct n1x_dla_accel_exec *r) {
    assert(fd >= 0 && image && bytes > 0 && ranges && count > 0 && writable && writable_count >= 2);
    log_event("EXEC");
    memset(r, 0, sizeof(*r));
    r->job_id = ++next_job;
    r->completed = 1;
    r->status = 0;
    return 0;
}
int n1x_dla_resubmit(int fd, unsigned long long id,
                     const struct n1x_dla_accel_patch *patches, unsigned int count,
                     struct n1x_dla_accel_exec *r) {
    assert(fd >= 0 && id > 0 && patches && count >= 2);
    log_event("REEXEC");
    memset(r, 0, sizeof(*r));
    r->job_id = ++next_job;
    r->completed = 1;
    r->status = 0;
    return 0;
}
int n1x_dla_read(int fd, unsigned long long job, unsigned long long offset,
                 void *out, unsigned long long bytes) {
    (void)offset;
    assert(fd >= 0 && job > 0 && out && bytes > 0);
    log_event("READ");
    /* Fill with 0x3c00 (1.0 in IEEE-754 FP16, or [0, 60] in INT8) */
    uint8_t *dst = (uint8_t *)out;
    for (unsigned long long i = 0; i < bytes; ++i) {
        dst[i] = (i & 1) ? 0x3c : 0x00;
    }
    return 0;
}
int n1x_dla_stats(int fd, unsigned long long job, unsigned long long offset,
                  unsigned long long bytes, struct n1x_dla_accel_stats *s) {
    assert(fd >= 0 && job > 0 && bytes > 0 && s);
    log_event("STATS");
    memset(s, 0, sizeof(*s));
    s->job_id = job;
    s->offset = offset;
    s->bytes = bytes;
    s->count = 2;
    return 0;
}
"""
        runner_py = f"""
import os, sys, json
from pathlib import Path
import numpy as np
sys.path.insert(0, {str(ROOT / "runtime")!r})
sys.path.insert(0, {str(ROOT / "scripts")!r})
from n1x_ort import InferenceSession
from test_n1x_ort import _build_three_stage_onnx, MNIST_ONNX, SUPERRES_ONNX

cache_dir = os.environ["TEST_ORT_CACHE"]

# 1. MNIST-12 single-stage DLA execution + REEXEC on second call
sess_m = InferenceSession(MNIST_ONNX, providers=[("N1xExecutionProvider", {{"cache_dir": cache_dir}})])
x_m = np.ones((1, 1, 28, 28), dtype=np.float32) * 0.5
y_m1 = sess_m.run(None, {{"Input3": x_m}})[0]
assert y_m1.shape == (1, 10), y_m1.shape
assert not sess_m.get_partition_summary()["stages"][0]["used_resubmit"]
y_m2 = sess_m.run(None, {{"Input3": x_m}})[0]
assert sess_m.get_partition_summary()["stages"][0]["used_resubmit"]
np.testing.assert_allclose(y_m1, 1.0, rtol=1e-5)
np.testing.assert_array_equal(y_m1, y_m2)

# 2. Super-Resolution-10 (DLA Stage 0 -> Host DepthToSpace Stage 1)
sess_sr = InferenceSession(
    SUPERRES_ONNX,
    providers=[("N1xExecutionProvider", {{"input_shapes": {{"input": [1, 1, 64, 64]}}, "cache_dir": cache_dir}})],
)
x_sr = np.ones((1, 1, 64, 64), dtype=np.float32) * 0.25
y_sr = sess_sr.run(None, {{"input": x_sr}})[0]
assert y_sr.shape == (1, 1, 192, 192), y_sr.shape
np.testing.assert_allclose(y_sr, 1.0, rtol=1e-5)

# 3. Three-stage DLA_0 -> Host_0 (Softmax) -> DLA_1 in INT8 mode
model_3 = _build_three_stage_onnx()
sess_3 = InferenceSession(
    model_3,
    providers=[("N1xExecutionProvider", {{"precision": "int8", "cache_dir": cache_dir}})],
)
x_3 = np.ones((1, 16, 8, 8), dtype=np.float32) * 0.1
y_3_first = sess_3.run(None, {{"input": x_3}})[0]
y_3_second = sess_3.run(None, {{"input": x_3}})[0]
assert y_3_first.shape == (1, 16, 8, 8)
summary_3 = sess_3.get_partition_summary()
assert summary_3["stages"][0]["used_resubmit"] and summary_3["stages"][2]["used_resubmit"]
np.testing.assert_array_equal(y_3_first, y_3_second)

# 4. Verify task-0-stats profiling and zero-copy mmap readback on N1xOrtModuleWrapper
from n1x_compiler import build_reference_dag, compile_dag
from n1x_ort import N1xOrtModuleWrapper
dag_st = build_reference_dag("relu")
dag_st["profile"] = {{"generate_stats": True}}
nvda_st = compile_dag(dag_st)
mod_st = N1xOrtModuleWrapper(nvda_st)
assert mod_st.has_stats and mod_st.stats_bytes > 0
in_sh_st = mod_st.inputs[0].shape
y_st = mod_st.run([np.ones(in_sh_st, dtype=np.float32)])[0]
assert y_st.shape == in_sh_st
raw_stats = mod_st.read_stats()
assert len(raw_stats) == mod_st.stats_bytes

mmap_path = Path(cache_dir) / "mmap_backing.bin"
pattern = (b"\\x00\\x3c" * ((4 * 1024 * 1024) // 2))
mmap_path.write_bytes(pattern)
os.environ["TEST_ORT_MMAP_FILE"] = str(mmap_path)
mod_mm = N1xOrtModuleWrapper(nvda_st)
y_mm = mod_mm.run([np.ones(in_sh_st, dtype=np.float32)])[0]
assert mod_mm.used_mmap
np.testing.assert_allclose(y_mm, 1.0, rtol=1e-5)
os.environ.pop("TEST_ORT_MMAP_FILE", None)
print("SUBPROCESS_OK")
"""
        with tempfile.TemporaryDirectory(prefix="n1x-ort-e2e-") as tmp:
            tdir = Path(tmp)
            mock_src = tdir / "mock_dla.c"
            mock_src.write_text(mock_dla_c)
            # Place mock libn1x_dla.so and a copy of libn1x_ort.so + n1x-ort-run in tdir
            # so $ORIGIN rpath resolves to the mocked libn1x_dla.so
            subprocess.run(
                [
                    "cc",
                    "-shared",
                    "-fPIC",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I" + str(ROOT / "include"),
                    "-I" + str(ROOT / "runtime"),
                    str(mock_src),
                    "-Wl,-soname,libn1x_dla.so",
                    "-o",
                    str(tdir / "libn1x_dla.so"),
                ],
                check=True,
            )
            events_file = tdir / "events.log"
            cache_dir = tdir / "cache"
            script_file = tdir / "run_e2e.py"
            script_file.write_text(runner_py)

            env = os.environ.copy()
            env["LD_PRELOAD"] = str(tdir / "libn1x_dla.so")
            env["N1X_DLA_DEVICE"] = "/dev/modeled-npu-only"
            env["TEST_ORT_EVENTS"] = str(events_file)
            env["TEST_ORT_CACHE"] = str(cache_dir)

            proc = subprocess.run(
                [sys.executable, str(script_file)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("SUBPROCESS_OK", proc.stdout)
            events = events_file.read_text().splitlines()
            # 4 session DLA modules + 2 wrapper modules = 6 EXECs, 3 REEXECs,
            # 7 session READs + 1 mod_st output READ + 1 mod_st stats READ = 9 READs, 1 STATS
            self.assertEqual(events.count("EXEC"), 6)
            self.assertEqual(events.count("REEXEC"), 3)
            self.assertEqual(events.count("READ"), 9)
            self.assertEqual(events.count("STATS"), 1)

            # Also verify C++ CLI `--run` mode against the mocked libn1x_dla.so
            nvda_file = next(cache_dir.glob("*.nvda"))
            cli_proc = subprocess.run(
                [str(ORT_RUN_BIN), "--run", str(nvda_file)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Run succeeded", cli_proc.stdout)


if __name__ == "__main__":
    unittest.main()
