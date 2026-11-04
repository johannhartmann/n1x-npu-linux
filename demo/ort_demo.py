#!/usr/bin/env python3
"""Heterogeneous ONNX Execution Provider demo (N1xInferenceSession).

Compiles, partitions, caches, and executes ONNX models (emotion-ferplus-8.onnx,
mnist-12.onnx, or super-resolution-10.onnx) across the N1X DLA NPU and Host CPU
reference fallback, producing a JSON summary and visual HTML report.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import platform
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import onnx
from onnx.reference import ReferenceEvaluator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from n1x_ort import InferenceSession, canonicalize_onnx_model

FERPLUS_LABELS = (
    "neutral",
    "happiness",
    "surprise",
    "sadness",
    "anger",
    "disgust",
    "fear",
    "contempt",
)

MODEL_ALIASES = {
    "emotion": ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx",
    "ferplus": ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx",
    "emotion-ferplus-8.onnx": ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx",
    "sources/demo-research/emotion-ferplus-8.onnx": ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx",
    "mnist": ROOT / "sources" / "demo-research" / "mnist-12.onnx",
    "mnist-12.onnx": ROOT / "sources" / "demo-research" / "mnist-12.onnx",
    "sources/demo-research/mnist-12.onnx": ROOT / "sources" / "demo-research" / "mnist-12.onnx",
    "superres": ROOT / "sources" / "demo-research" / "super-resolution-10.onnx",
    "super-resolution-10.onnx": ROOT / "sources" / "demo-research" / "super-resolution-10.onnx",
    "sources/demo-research/super-resolution-10.onnx": ROOT / "sources" / "demo-research" / "super-resolution-10.onnx",
}


def _encode_grayscale_png_data_url(arr_2d: np.ndarray, scale_max: float = 255.0) -> str:
    """Encode a 2D float/uint8 array into a lossless 8-bit grayscale PNG data URL."""
    norm = np.clip(np.asarray(arr_2d, dtype=np.float32) * (255.0 / max(1e-6, scale_max)), 0.0, 255.0)
    u8 = np.ascontiguousarray(np.round(norm).astype(np.uint8))
    h, w = u8.shape

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    raw_scanlines = b"".join(b"\x00" + u8[row].tobytes() for row in range(h))
    idat = zlib.compress(raw_scanlines, level=9)
    png_bytes = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _make_ferplus_face(
    mouth_curve: float = 0.0,
    mouth_open: float = 0.0,
    brow_slope: float = 0.0,
    eye_open: float = 1.0,
    brow_y: float = 18.0,
    mouth_y: float = 48.0,
) -> np.ndarray:
    """Synthesize a deterministic 64x64 grayscale facial expression crop in [0, 255]."""
    yy, xx = np.mgrid[0:64, 0:64].astype(np.float32)
    cx, cy = 31.5, 33.0
    rx, ry = 24.0, 28.0
    dist2 = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    img = np.where(dist2 <= 1.0, 185.0 - 45.0 * dist2, 40.0)
    img += 18.0 * np.exp(-(((xx - 20.0) / 7.0) ** 2 + ((yy - 36.0) / 6.0) ** 2))
    img += 18.0 * np.exp(-(((xx - 43.0) / 7.0) ** 2 + ((yy - 36.0) / 6.0) ** 2))
    img += 16.0 * np.exp(-(((xx - 31.5) / 3.0) ** 2 + ((yy - 32.0) / 8.0) ** 2))
    img -= 25.0 * np.exp(-(((xx - 29.0) / 2.0) ** 2 + ((yy - 39.0) / 1.5) ** 2))
    img -= 25.0 * np.exp(-(((xx - 34.0) / 2.0) ** 2 + ((yy - 39.0) / 1.5) ** 2))
    for ex in (21.0, 42.0):
        ey = 24.5
        erx, ery = 5.0, 2.8 * eye_open
        ed = ((xx - ex) / erx) ** 2 + ((yy - ey) / max(0.8, ery)) ** 2
        img = np.where(ed < 1.0, 225.0, img)
        pd = ((xx - ex) / 2.2) ** 2 + ((yy - ey) / max(0.8, min(2.2, ery))) ** 2
        img = np.where(pd < 1.0, 30.0, img)
    for sign, bx in ((-1.0, 21.0), (1.0, 42.0)):
        by = brow_y + brow_slope * sign * (xx - bx) * 0.35
        bd = ((xx - bx) / 6.5) ** 2 + ((yy - by) / 1.6) ** 2
        img = np.where(bd < 1.0, 45.0, img)
    mx, my = 31.5, mouth_y
    m_y_center = my - mouth_curve * ((xx - mx) / 10.0) ** 2 * 5.0
    m_half_h = 1.5 + mouth_open * 3.5 * np.maximum(0.0, 1.0 - ((xx - mx) / 9.0) ** 2)
    in_mouth = (np.abs(xx - mx) < 10.0) & (np.abs(yy - m_y_center) < m_half_h)
    img = np.where(in_mouth, 35.0, img)
    if mouth_curve > 0.3:
        in_teeth = (np.abs(xx - mx) < 8.0) & (np.abs(yy - m_y_center) < m_half_h * 0.55)
        img = np.where(in_teeth, 230.0, img)
    return np.clip(img, 0.0, 255.0).astype(np.float32).reshape(1, 1, 64, 64)


def build_ferplus_gallery() -> list[tuple[str, np.ndarray]]:
    """Return 6 deterministic 64x64 face presets spanning 6 FER+ emotion classes."""
    return [
        ("Broad Smile", _make_ferplus_face(mouth_curve=1.2, mouth_open=0.5, brow_slope=-0.2, eye_open=0.9)),
        ("Calm Neutral", _make_ferplus_face(mouth_curve=0.0, mouth_open=0.0, brow_slope=0.0, eye_open=1.0)),
        ("Wide-Eyed Surprise", _make_ferplus_face(mouth_curve=0.0, mouth_open=1.3, brow_slope=-0.5, eye_open=1.5)),
        ("Downcast Sadness", _make_ferplus_face(mouth_curve=-0.8, mouth_open=0.0, brow_slope=1.2, eye_open=0.5, brow_y=16.5)),
        ("Frowning Stern", _make_ferplus_face(mouth_curve=-1.0, mouth_open=0.0, brow_slope=-0.8, eye_open=0.8)),
        ("Tense Worried", _make_ferplus_face(mouth_curve=-0.6, mouth_open=0.2, brow_slope=1.2, eye_open=0.85)),
    ]


def _softmax(logits: np.ndarray) -> np.ndarray:
    x = np.asarray(logits, dtype=np.float64).reshape(-1)
    shifted = np.exp(x - np.max(x))
    return (shifted / np.sum(shifted)).astype(np.float64)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    af = np.asarray(a, dtype=np.float64).reshape(-1)
    bf = np.asarray(b, dtype=np.float64).reshape(-1)
    denom = float(np.linalg.norm(af) * np.linalg.norm(bf))
    return float(np.dot(af, bf) / denom) if denom > 1e-12 else 1.0


def _build_default_inputs(
    model_path: Path, shape_overrides: dict[str, list[int]]
) -> tuple[dict[str, np.ndarray], list[dict[str, np.ndarray]]]:
    model = onnx.load(str(model_path))
    if model_path.name == "emotion-ferplus-8.onnx":
        gallery = build_ferplus_gallery()
        calib_samples = [{"Input3": arr} for _, arr in gallery]
        return calib_samples[0], calib_samples

    rng = np.random.default_rng(12)
    calib_samples = []
    for _ in range(4):
        feed: dict[str, np.ndarray] = {}
        for vi in model.graph.input:
            if any(init.name == vi.name for init in model.graph.initializer):
                continue
            if vi.name in shape_overrides:
                shape = list(shape_overrides[vi.name])
            else:
                shape = [int(d.dim_value) if d.dim_value > 0 else 1 for d in vi.type.tensor_type.shape.dim]
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


def _resolve_model_arg(raw_model: str) -> Path | None:
    key = raw_model.strip().lower()
    if key in MODEL_ALIASES:
        return MODEL_ALIASES[key]
    for name, path in MODEL_ALIASES.items():
        if key.endswith("/" + name):
            return path
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        type=str,
        default="mnist",
        help="Demo model alias or filename ('emotion', 'mnist', 'superres')",
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
        help="New directory for execution report and JSON output",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("N1X_DLA_DEVICE", "/dev/accel/accel0"),
        help="NPU device path (default: /dev/accel/accel0)",
    )
    args = parser.parse_args()

    model_path = _resolve_model_arg(args.model)
    if model_path is None or not model_path.is_file():
        parser.error(f"Unsupported or missing ONNX demo model: {args.model}")

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
    cos_sim = _cosine_similarity(out_arr, ref_arr)

    summary = sess.get_partition_summary()
    result: dict[str, Any] = {
        "model": str(model_path),
        "precision": args.precision,
        "kernel": platform.release(),
        "compile_or_cache_ms": round(compile_ms, 3),
        "first_run_exec_ms": round(exec_ms, 3),
        "second_run_reexec_ms": round(reexec_ms, 3),
        "output_shape": list(out_arr.shape),
        "maximum_absolute_error": max_abs_err,
        "rmse": rmse,
        "cosine_similarity": round(cos_sim, 6),
        "partition_summary": summary,
    }

    gallery_html = ""
    if model_path.name == "emotion-ferplus-8.onnx":
        gallery_entries: list[dict[str, Any]] = []
        cards_html: list[str] = []
        for preset_name, face_arr in build_ferplus_gallery():
            t0 = time.monotonic()
            npu_logits = np.asarray(sess.run(None, {"Input3": face_arr})[0], dtype=np.float32).reshape(-1)
            lat_ms = (time.monotonic() - t0) * 1000.0
            cpu_logits = np.asarray(ref_eval.run(None, {"Input3": face_arr})[0], dtype=np.float32).reshape(-1)
            npu_probs = _softmax(npu_logits)
            cpu_probs = _softmax(cpu_logits)
            npu_top = int(np.argmax(npu_probs))
            cpu_top = int(np.argmax(cpu_probs))
            f_diff = npu_logits.astype(np.float64) - cpu_logits.astype(np.float64)
            f_max_err = float(np.max(np.abs(f_diff)))
            f_cos = _cosine_similarity(npu_logits, cpu_logits)

            gallery_entries.append(
                {
                    "preset": preset_name,
                    "reexec_ms": round(lat_ms, 3),
                    "npu_top1_label": FERPLUS_LABELS[npu_top],
                    "npu_top1_prob": round(float(npu_probs[npu_top]), 4),
                    "cpu_top1_label": FERPLUS_LABELS[cpu_top],
                    "cpu_top1_prob": round(float(cpu_probs[cpu_top]), 4),
                    "top1_match": bool(npu_top == cpu_top),
                    "cosine_similarity": round(f_cos, 6),
                    "max_abs_logit_error": round(f_max_err, 6),
                }
            )

            data_url = _encode_grayscale_png_data_url(face_arr[0, 0], scale_max=255.0)
            top_indices = list(np.argsort(-cpu_probs)[:4])
            bars_html = []
            for idx in top_indices:
                lbl = FERPLUS_LABELS[int(idx)]
                npu_pct = float(npu_probs[idx]) * 100.0
                cpu_pct = float(cpu_probs[idx]) * 100.0
                bars_html.append(
                    f'<div class="bar-row">'
                    f'<div class="bar-label"><span>{html.escape(lbl)}</span>'
                    f'<span>NPU {npu_pct:.1f}% / CPU {cpu_pct:.1f}%</span></div>'
                    f'<div class="bar-track"><div class="bar-fill" style="width:{min(100.0, npu_pct):.1f}%"></div></div>'
                    f"</div>"
                )
            match_badge = (
                '<span class="badge pass">TOP-1 MATCH</span>'
                if npu_top == cpu_top
                else '<span class="badge warn">TOP-1 DIFF</span>'
            )
            cards_html.append(
                f'<div class="card">'
                f'<div class="card-head">'
                f'<img src="{data_url}" alt="{html.escape(preset_name)}" width="96" height="96">'
                f'<div><h3>{html.escape(preset_name)}</h3>'
                f'<div class="pred">{html.escape(FERPLUS_LABELS[npu_top].upper())} ({npu_probs[npu_top]*100:.1f}%)</div>'
                f'<div class="meta">{match_badge} · {lat_ms:.2f} ms REEXEC_IO · cos={f_cos:.5f}</div>'
                f"</div></div>"
                f'{"".join(bars_html)}'
                f"</div>"
            )
        result["emotion_gallery"] = gallery_entries
        gallery_html = (
            '<h2>FER+ 8-Class Facial Emotion Gallery (Live NPU Execution)</h2>'
            '<div class="grid">' + "".join(cards_html) + "</div>"
        )

    (dest / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    title = f"N1X Heterogeneous ONNX EP: {model_path.name} ({args.precision.upper()})"
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{background:#0f1117;color:#e6edf3;font:15px/1.5 system-ui,-apple-system,sans-serif;margin:32px;max-width:1200px}}
h1{{margin:0 0 8px;font-size:24px}}
h2{{margin:28px 0 14px;font-size:18px;color:#8b949e;border-bottom:1px solid #21262d;padding-bottom:6px}}
.summary{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.stat{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px}}
.stat b{{display:block;font-size:12px;color:#8b949e;text-transform:uppercase}}
.stat span{{font-size:18px;font-weight:600;color:#58a6ff}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;margin:16px 0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}}
.card-head{{display:flex;gap:14px;align-items:center;margin-bottom:12px}}
.card-head img{{image-rendering:pixelated;border-radius:8px;border:1px solid #30363d;background:#000}}
.card-head h3{{margin:0;font-size:16px}}
.pred{{font-size:18px;font-weight:700;color:#3fb950;margin:2px 0}}
.meta{{font-size:12px;color:#8b949e}}
.badge{{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:700}}
.badge.pass{{background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb}}
.badge.warn{{background:#d2992233;color:#d29922;border:1px solid #d29922}}
.bar-row{{margin:6px 0}}
.bar-label{{display:flex;justify-content:space-between;font-size:12px;color:#c9d1d9;margin-bottom:2px}}
.bar-track{{height:7px;background:#21262d;border-radius:4px;overflow:hidden}}
.bar-fill{{height:100%;background:linear-gradient(90deg,#238636,#3fb950);border-radius:4px}}
pre{{white-space:pre-wrap;background:#161b22;border:1px solid #30363d;padding:16px;border-radius:8px;font-size:13px}}
</style>
<h1>{html.escape(title)}</h1>
<div class="summary">
  <div class="stat"><b>DLA / Host Stages</b><span>{summary["dla_stages"]} DLA / {summary["host_stages"]} Host</span></div>
  <div class="stat"><b>Compile / Cache</b><span>{compile_ms:.2f} ms</span></div>
  <div class="stat"><b>First Run (EXEC)</b><span>{exec_ms:.2f} ms</span></div>
  <div class="stat"><b>Retained Run (REEXEC_IO)</b><span>{reexec_ms:.2f} ms</span></div>
  <div class="stat"><b>Max Abs Error vs FP32</b><span>{max_abs_err:.5g}</span></div>
  <div class="stat"><b>Cosine Similarity</b><span>{cos_sim:.6f}</span></div>
</div>
{gallery_html}
<h2>Execution Summary (JSON)</h2>
<pre>{html.escape(json.dumps(result, indent=2))}</pre>
"""
    (dest / "report.html").write_text(page)
    print(
        f"PASS: {model_path.name} ({args.precision}) -> {dest / 'report.html'} "
        f"(DLA stages={summary['dla_stages']}, Host stages={summary['host_stages']}, "
        f"exec={exec_ms:.2f}ms, reexec={reexec_ms:.2f}ms, "
        f"max_err={max_abs_err:.6g}, rmse={rmse:.6g}, cos={cos_sim:.6f})",
        flush=True,
    )


if __name__ == "__main__":
    main()
