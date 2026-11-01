#!/usr/bin/env python3
"""Heterogeneous ONNX Execution Provider demo (N1xInferenceSession).

Compiles, partitions, caches, and executes ONNX models (e.g. mnist-12.onnx or
super-resolution-10.onnx) across the N1X DLA NPU and Host CPU reference fallback,
producing a JSON receipt and HTML report.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np
import onnx
from onnx.reference import ReferenceEvaluator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from n1x_ort import InferenceSession, canonicalize_onnx_model


def _build_default_inputs(
    model_path: Path, shape_overrides: dict[str, list[int]]
) -> tuple[dict[str, np.ndarray], list[dict[str, np.ndarray]]]:
    model = onnx.load(str(model_path))
    rng = np.random.default_rng(12)
    calib_samples: list[dict[str, np.ndarray]] = []
    for sample_idx in range(4):
        feed: dict[str, np.ndarray] = {}
        for vi in model.graph.input:
            if any(init.name == vi.name for init in model.graph.initializer):
                continue
            if vi.name in shape_overrides:
                shape = list(shape_overrides[vi.name])
            else:
                shape = []
                for dim in vi.type.tensor_type.shape.dim:
                    shape.append(int(dim.dim_value) if dim.dim_value > 0 else 1)
            if shape == [1, 1, 28, 28]:
                img = np.zeros((1, 1, 28, 28), dtype=np.float32)
                img[0, 0, 6:22, 12:16] = 1.0
                img[0, 0, 6:10, 8:20] = 0.8
                img += rng.uniform(0.0, 0.15, size=shape).astype(np.float32)
                feed[vi.name] = img
            else:
                feed[vi.name] = rng.uniform(0.1, 0.9, size=shape).astype(np.float32)
        calib_samples.append(feed)
    return calib_samples[0], calib_samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "sources" / "demo-research" / "mnist-12.onnx",
        help="Path to ONNX model (default: sources/demo-research/mnist-12.onnx)",
    )
    parser.add_argument(
        "--precision",
        choices=("fp16", "int8"),
        default="fp16",
        help="DLA compilation precision (fp16 or auto-calibrated int8)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New directory for execution report and JSON evidence",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("N1X_DLA_DEVICE", "/dev/accel/accel0"),
        help="NPU device path (default: /dev/accel/accel0)",
    )
    args = parser.parse_args()

    model_path = args.model.resolve()
    if not model_path.is_file():
        parser.error(f"ONNX model not found: {model_path}")

    dest = args.output_dir.resolve()
    dest.mkdir(parents=True, exist_ok=False)

    shape_overrides: dict[str, list[int]] = {}
    if model_path.name == "super-resolution-10.onnx":
        shape_overrides["input"] = [1, 1, 64, 64]

    test_feed, calib_samples = _build_default_inputs(model_path, shape_overrides)

    provider_opts: dict[str, object] = {
        "precision": args.precision,
        "device_path": args.device,
        "input_shapes": shape_overrides,
    }
    if args.precision == "int8":
        provider_opts["calibration_data"] = calib_samples

    t_compile0 = time.monotonic()
    sess = InferenceSession(
        model_path,
        providers=[("N1xExecutionProvider", provider_opts), "CPUExecutionProvider"],
    )
    compile_ms = (time.monotonic() - t_compile0) * 1000.0

    t_run0 = time.monotonic()
    outputs_first = sess.run(None, test_feed)
    exec_ms = (time.monotonic() - t_run0) * 1000.0

    t_run1 = time.monotonic()
    outputs_second = sess.run(None, test_feed)
    reexec_ms = (time.monotonic() - t_run1) * 1000.0

    for a, b in zip(outputs_first, outputs_second):
        np.testing.assert_array_equal(a, b)

    ref_model = (
        canonicalize_onnx_model(model_path, input_shapes=shape_overrides)[0]
        if shape_overrides
        else onnx.load(str(model_path))
    )
    ref_eval = ReferenceEvaluator(ref_model)
    ref_outputs = ref_eval.run(None, test_feed)

    out_arr = np.asarray(outputs_first[0], dtype=np.float32)
    ref_arr = np.asarray(ref_outputs[0], dtype=np.float32)
    diff = out_arr.astype(np.float64) - ref_arr.astype(np.float64)
    max_abs_err = float(np.max(np.abs(diff)))
    rmse = float(np.sqrt(np.mean(diff * diff)))

    summary = sess.get_partition_summary()
    result = {
        "model": str(model_path),
        "precision": args.precision,
        "kernel": platform.release(),
        "compile_or_cache_ms": round(compile_ms, 3),
        "first_run_exec_ms": round(exec_ms, 3),
        "second_run_reexec_ms": round(reexec_ms, 3),
        "output_shape": list(out_arr.shape),
        "maximum_absolute_error": max_abs_err,
        "rmse": rmse,
        "partition_summary": summary,
    }

    (dest / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    title = f"N1X Heterogeneous ONNX EP: {model_path.name} ({args.precision.upper()})"
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{background:#111;color:#eee;font:16px system-ui;margin:32px}}pre{{white-space:pre-wrap;background:#1b1b1b;padding:16px;border-radius:8px}}</style>
<h1>{html.escape(title)}</h1>
<p>DLA stages: {summary["dla_stages"]} | Host stages: {summary["host_stages"]} | First run: {exec_ms:.2f} ms | Second run (REEXEC_IO): {reexec_ms:.2f} ms</p>
<p>Max abs error vs FP32 ONNX ReferenceEvaluator: {max_abs_err:.6g} | RMSE: {rmse:.6g}</p>
<pre>{html.escape(json.dumps(result, indent=2))}</pre>
"""
    (dest / "report.html").write_text(page)
    print(
        f"PASS: {model_path.name} ({args.precision}) -> {dest / 'report.html'} "
        f"(DLA stages={summary['dla_stages']}, Host stages={summary['host_stages']}, "
        f"max_err={max_abs_err:.6g}, rmse={rmse:.6g})",
        flush=True,
    )


if __name__ == "__main__":
    main()
