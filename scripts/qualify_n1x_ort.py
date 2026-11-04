#!/usr/bin/env python3
"""Physical N1X NPU (/dev/accel/accel0) qualification of N1xInferenceSession (Heterogeneous ONNX EP).

Executes:
1. mnist-12.onnx (1-stage full-graph DLA compilation, FP16 + auto-calibrated INT8, REEXEC_IO reuse)
2. super-resolution-10.onnx (2-stage heterogeneous DLA + Host execution, REEXEC_IO reuse)
3. 3-stage DLA -> Host (ReduceMean) -> DLA graph in FP16 and auto-calibrated INT8
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import onnx
from onnx.reference import ReferenceEvaluator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "demo"))

from n1x_dla import Device as Transport
from n1x_ort import InferenceSession, canonicalize_onnx_model
from ort_demo import FERPLUS_LABELS, build_ferplus_gallery
from test_n1x_ort import _build_three_stage_onnx

ENDPOINT = Path("/sys/bus/platform/devices/NVDA200A:00")
SROOT = Path("/sys/bus/platform/devices/NVDA8600:00")
MODULE_SYS = Path("/sys/module/n1x_npu_bootstrap")
ACCEL_NODE = "/dev/accel/accel0"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fields(text: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in text.split() if "=" in item)


def ensure_npu_started() -> bool:
    if MODULE_SYS.exists():
        st = _fields((ENDPOINT / "status").read_text()) if (ENDPOINT / "status").exists() else {}
        if st.get("task_verified") == "1" and Path(ACCEL_NODE).exists():
            return False
        subprocess.run(["/usr/bin/rmmod", "n1x_npu_bootstrap"], check=True)
    for dev in (ENDPOINT, SROOT):
        group_type = dev / "iommu_group/type"
        if not (dev / "driver").exists() and group_type.read_text().strip() == "identity":
            group_type.write_text("DMA\n")
    ko = ROOT / "driver/n1x_npu_bootstrap.ko"
    subprocess.run(["/usr/bin/insmod", str(ko)], check=True)
    st = _fields((ENDPOINT / "status").read_text())
    if st.get("task_verified") != "1" or not Path(ACCEL_NODE).exists():
        raise RuntimeError(f"in-kernel autostart did not set task_verified=1: {st}")
    return True


def stop_and_unload_npu() -> dict[str, str]:
    subprocess.run(["/usr/bin/rmmod", "n1x_npu_bootstrap"], check=True)
    return {"unloaded": "1"}


def qualify_mnist12(tmpdir: str) -> dict:
    mnist_path = ROOT / "sources" / "demo-research" / "mnist-12.onnx"
    ref_eval = ReferenceEvaluator(onnx.load(str(mnist_path)))

    rng = np.random.default_rng(12)
    samples = []
    for _ in range(4):
        img = np.zeros((1, 1, 28, 28), dtype=np.float32)
        img[0, 0, 6:22, 12:16] = 1.0
        img[0, 0, 6:10, 8:20] = 0.8
        img += rng.uniform(0.0, 0.15, size=(1, 1, 28, 28)).astype(np.float32)
        samples.append({"Input3": img})

    with InferenceSession(
        mnist_path,
        providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
    ) as sess_fp16:
        t0 = time.monotonic()
        out_fp16_0 = sess_fp16.run(None, samples[0])[0]
        exec_ms = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        out_fp16_1 = sess_fp16.run(None, samples[0])[0]
        reexec_ms = (time.monotonic() - t1) * 1000.0
        ref_0 = np.asarray(ref_eval.run(None, samples[0])[0], dtype=np.float32)

        fp16_max_err = float(np.max(np.abs(out_fp16_0 - ref_0)))
        fp16_rmse = float(np.sqrt(np.mean((out_fp16_0 - ref_0) ** 2)))
        fp16_summary = sess_fp16.get_partition_summary()
        resubmit_ok = bool(fp16_summary["stages"][0]["used_resubmit"])
        np.testing.assert_array_equal(out_fp16_0, out_fp16_1)

    with InferenceSession(
        mnist_path,
        providers=[
            (
                "N1xExecutionProvider",
                {"precision": "int8", "calibration_data": samples, "cache_dir": tmpdir},
            ),
            "CPUExecutionProvider",
        ],
    ) as sess_int8:
        out_int8_0 = sess_int8.run(None, samples[0])[0]
    cos_sim = float(
        np.dot(out_int8_0.reshape(-1), ref_0.reshape(-1))
        / (np.linalg.norm(out_int8_0) * np.linalg.norm(ref_0) + 1e-12)
    )
    top1_match = int(np.argmax(out_fp16_0)) == int(np.argmax(ref_0)) and int(
        np.argmax(out_int8_0)
    ) == int(np.argmax(ref_0))
    return {
        "model_sha256": _sha256(mnist_path),
        "dla_stages": fp16_summary["dla_stages"],
        "host_stages": fp16_summary["host_stages"],
        "nvda_bytes": fp16_summary["stages"][0]["nvda_bytes"],
        "fp16_max_abs_err": fp16_max_err,
        "fp16_rmse": fp16_rmse,
        "fp16_exec_ms": round(exec_ms, 3),
        "fp16_reexec_ms": round(reexec_ms, 3),
        "int8_cosine_sim": cos_sim,
        "top1_match": top1_match,
        "used_resubmit": resubmit_ok,
        "ref_logits": [round(float(x), 4) for x in ref_0.reshape(-1)],
        "fp16_logits": [round(float(x), 4) for x in out_fp16_0.reshape(-1)],
        "int8_logits": [round(float(x), 4) for x in out_int8_0.reshape(-1)],
    }


def qualify_superres10(tmpdir: str) -> dict:
    sr_path = ROOT / "sources" / "demo-research" / "super-resolution-10.onnx"
    ref_model, _ = canonicalize_onnx_model(sr_path, input_shapes={"input": [1, 1, 64, 64]})
    ref_eval = ReferenceEvaluator(ref_model)
    rng = np.random.default_rng(77)
    inp = rng.uniform(0.1, 0.9, size=(1, 1, 64, 64)).astype(np.float32)
    feed = {"input": inp}

    with InferenceSession(
        sr_path,
        providers=[
            (
                "N1xExecutionProvider",
                {"input_shapes": {"input": [1, 1, 64, 64]}, "cache_dir": tmpdir},
            ),
            "CPUExecutionProvider",
        ],
    ) as sess_fp16:
        t0 = time.monotonic()
        out_fp16 = sess_fp16.run(None, feed)[0]
        exec_ms = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        out_fp16_second = sess_fp16.run(None, feed)[0]
        reexec_ms = (time.monotonic() - t1) * 1000.0
        ref_out = np.asarray(ref_eval.run(None, feed)[0], dtype=np.float32)
        max_err = float(np.max(np.abs(out_fp16 - ref_out)))
        rmse = float(np.sqrt(np.mean((out_fp16 - ref_out) ** 2)))
        np.testing.assert_array_equal(out_fp16, out_fp16_second)
        summary = sess_fp16.get_partition_summary()
    return {
        "model_sha256": _sha256(sr_path),
        "output_shape": list(out_fp16.shape),
        "fp16_max_abs_err": max_err,
        "fp16_rmse": rmse,
        "fp16_exec_ms": round(exec_ms, 3),
        "fp16_reexec_ms": round(reexec_ms, 3),
        "dla_stages": summary["dla_stages"],
        "host_stages": summary["host_stages"],
        "nvda_bytes": summary["stages"][0]["nvda_bytes"],
        "used_resubmit": summary["stages"][0]["used_resubmit"],
    }


def qualify_three_stage(tmpdir: str) -> dict:
    model = _build_three_stage_onnx()
    ref_eval = ReferenceEvaluator(model)
    rng = np.random.default_rng(55)
    calib = [
        {"input": rng.uniform(-0.5, 0.5, size=(1, 16, 8, 8)).astype(np.float32)}
        for _ in range(4)
    ]
    test_feed = calib[0]
    ref_out = np.asarray(ref_eval.run(None, test_feed)[0], dtype=np.float32)

    with InferenceSession(
        model,
        providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
    ) as sess_fp16:
        out_fp16 = sess_fp16.run(None, test_feed)[0]
        fp16_err = float(np.max(np.abs(out_fp16 - ref_out)))
        stages_list = [s["kind"] for s in sess_fp16.get_partition_summary()["stages"]]

    with InferenceSession(
        model,
        providers=[
            (
                "N1xExecutionProvider",
                {"precision": "int8", "calibration_data": calib, "cache_dir": tmpdir},
            ),
            "CPUExecutionProvider",
        ],
    ) as sess_int8:
        out_int8 = sess_int8.run(None, test_feed)[0]
        int8_mae = float(np.mean(np.abs(out_int8 - ref_out)))
    return {
        "fp16_max_abs_err": fp16_err,
        "int8_mae": int8_mae,
        "stages": stages_list,
    }


def qualify_emotion_ferplus8(tmpdir: str) -> dict | None:
    em_path = ROOT / "sources" / "demo-research" / "emotion-ferplus-8.onnx"
    if not em_path.is_file():
        return None
    ref_eval = ReferenceEvaluator(onnx.load(str(em_path)))
    gallery = build_ferplus_gallery()
    calib_samples = [{"Input3": arr} for _, arr in gallery]
    feed0 = calib_samples[0]

    with InferenceSession(
        em_path,
        providers=[("N1xExecutionProvider", {"cache_dir": tmpdir}), "CPUExecutionProvider"],
    ) as sess_fp16:
        t0 = time.monotonic()
        out_fp16_0 = sess_fp16.run(None, feed0)[0]
        exec_ms = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        out_fp16_0_re = sess_fp16.run(None, feed0)[0]
        reexec_ms = (time.monotonic() - t1) * 1000.0
        np.testing.assert_array_equal(out_fp16_0, out_fp16_0_re)
        summary_fp16 = sess_fp16.get_partition_summary()
        gallery_matches = 0
        for _, face_arr in gallery:
            npu_l = sess_fp16.run(None, {"Input3": face_arr})[0].reshape(-1)
            cpu_l = np.asarray(ref_eval.run(None, {"Input3": face_arr})[0], dtype=np.float32).reshape(-1)
            if int(np.argmax(npu_l)) == int(np.argmax(cpu_l)):
                gallery_matches += 1

    with InferenceSession(
        em_path,
        providers=[
            (
                "N1xExecutionProvider",
                {"precision": "int8", "calibration_data": calib_samples, "cache_dir": tmpdir},
            ),
            "CPUExecutionProvider",
        ],
    ) as sess_int8:
        out_int8_0 = sess_int8.run(None, feed0)[0]
        summary_int8 = sess_int8.get_partition_summary()

    ref_0 = np.asarray(ref_eval.run(None, feed0)[0], dtype=np.float32)
    fp16_err = float(np.max(np.abs(out_fp16_0 - ref_0)))
    fp16_rmse = float(np.sqrt(np.mean((out_fp16_0 - ref_0) ** 2)))
    v_ref = ref_0.reshape(-1).astype(np.float64)
    v_i8 = out_int8_0.reshape(-1).astype(np.float64)
    cos_sim = float(np.dot(v_ref, v_i8) / (np.linalg.norm(v_ref) * np.linalg.norm(v_i8)))
    top1_label = FERPLUS_LABELS[int(np.argmax(out_fp16_0))]
    return {
        "model_sha256": _sha256(em_path),
        "fp16_nvda_bytes": summary_fp16["stages"][0]["nvda_bytes"],
        "int8_nvda_bytes": summary_int8["stages"][0]["nvda_bytes"],
        "fp16_max_abs_err": fp16_err,
        "fp16_rmse": fp16_rmse,
        "fp16_exec_ms": round(exec_ms, 3),
        "fp16_reexec_ms": round(reexec_ms, 3),
        "int8_cosine_sim": round(cos_sim, 6),
        "top1_label": top1_label,
        "gallery_top1_matches": f"{gallery_matches}/{len(gallery)}",
        "used_resubmit": summary_fp16["stages"][0]["used_resubmit"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", type=Path, help="Optional JSON summary output path")
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the NPU module loaded and running even if started by this script",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("requires root / CAP_SYS_RAWIO")

    started = ensure_npu_started()
    shutdown_info = None
    try:
        with Transport(ACCEL_NODE) as transport:
            transport_before = transport.info()
        with tempfile.TemporaryDirectory() as tmpdir:
            mnist_res = qualify_mnist12(tmpdir)
            sr_res = qualify_superres10(tmpdir)
            three_stage_res = qualify_three_stage(tmpdir)
            emotion_res = qualify_emotion_ferplus8(tmpdir)
        with Transport(ACCEL_NODE) as transport:
            transport_after = transport.info()
    finally:
        gc.collect()
        if started and not args.keep_running:
            shutdown_info = stop_and_unload_npu()

    summary = {
        "schema": "n1x-ort-hardware-qualification-v1",
        "date": "2026-11-01",
        "kernel": platform.release(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "platform_profile": Path("/sys/firmware/acpi/platform_profile").read_text().strip(),
        "artifacts": {
            "libn1x_ort_sha256": _sha256(ROOT / "runtime" / "libn1x_ort.so"),
            "libn1x_compiler_sha256": _sha256(ROOT / "runtime" / "libn1x_compiler.so"),
            "libn1x_dla_sha256": _sha256(ROOT / "runtime" / "libn1x_dla.so"),
        },
        "transport_before": transport_before,
        "transport_after": transport_after,
        "mnist12": mnist_res,
        "superres10": sr_res,
        "emotion_ferplus8": emotion_res,
        "three_stage_dla_host_dla": three_stage_res,
        "shutdown": shutdown_info,
    }

    assert summary["mnist12"]["fp16_max_abs_err"] < 0.05, summary["mnist12"]
    assert summary["mnist12"]["top1_match"], summary["mnist12"]
    assert summary["mnist12"]["used_resubmit"], summary["mnist12"]
    assert summary["superres10"]["fp16_max_abs_err"] < 0.005, summary["superres10"]
    assert summary["superres10"]["used_resubmit"], summary["superres10"]
    assert summary["three_stage_dla_host_dla"]["fp16_max_abs_err"] < 0.005, summary[
        "three_stage_dla_host_dla"
    ]

    if args.output_json:
        out_path = args.output_json.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2) + "\n")
        owner = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
        if owner is not None:
            os.chown(out_path, int(owner), -1)

    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
