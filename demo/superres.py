#!/usr/bin/env python3
"""3x full-image super-resolution using real N1X DLA through NVIDIA cuDLA.

No CPU fallback for learned inference. CPU ONNX execution checks the result.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import sys
import time
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from cudla_python import Device,FRONTEND_SHA256
from superres_image import CpuReference,MODEL_SHA256,assemble_rgb,fixed_tiles,open_luminance,tiled_planes
from n1x_dla import Device as Transport
from loadable import Model

LOADABLE_SHA256='c1ffde5d1eb9c7486a08ecc1470a1b633a11c7a71411f8fb36064d16dbcfc1c0'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',type=Path,help='PNG/JPEG image, at least 64 pixels on both axes')
    parser.add_argument('--output-dir',type=Path,required=True,help='new directory for DLA output, comparison and evidence')
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('experimental driver requires root/CAP_SYS_RAWIO; use ./demo-superres')
    if os.environ.get('LD_PRELOAD'):parser.error('remove LD_PRELOAD before hardware qualification')
    if Path('/sys/firmware/acpi/platform_profile').read_text().strip()!='performance':parser.error('select performance profile first')
    path=ROOT/'results/native-compile-t254-superres/superres.nvda'
    model=Model.from_file(path)
    if model.sha256!=LOADABLE_SHA256:parser.error('model differs from qualified super-resolution artifact')
    with Transport('/dev/accel/accel0') as transport:before=transport.info()
    if not before['capabilities']&4:parser.error('loaded module lacks completed-task I/O reuse; load reviewed module after a fresh boot')
    required=(len(model.image)+8192+4095)&~4095
    if required>before['retain_limit']-before['retained_bytes']:parser.error('insufficient retained-memory budget; no work submitted')
    rgb,luminance,cb,cr=open_luminance(args.image)
    height,width=luminance.shape
    plan=list(fixed_tiles(height,width,64))
    # cuDLA sees FP16 inputs; use precisely the same quantized input in the
    # independent FP32-weight reference to isolate backend arithmetic error.
    luminance=luminance.astype(np.float16).astype(np.float32)
    destination=args.output_dir.resolve();destination.mkdir(parents=True,exist_ok=False)
    owner=int(os.environ['PKEXEC_UID']) if 'PKEXEC_UID' in os.environ else None
    def owned(file):
        if owner is not None:os.chown(file,owner,-1)
    owned(destination)
    def record(name,data):
        with (destination/name).open('x') as out:
            json.dump(data,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        owned(destination/name)
    def save_image(name,image):
        image.save(destination/name);owned(destination/name)
    metadata=dict(input=str(args.image.resolve()),input_shape=[height,width],output_shape=[height*3,width*3],
                  tiles=len(plan),tile_shape=[64,64],halo=5,model_sha256=model.sha256,
                  onnx_sha256=MODEL_SHA256,frontend_sha256=FRONTEND_SHA256,
                  backend_sha256=hashlib.sha256((ROOT/'runtime/cudla/libnvcudla.so').read_bytes()).hexdigest(),
                  kernel=platform.release(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  module_srcversion=Path('/sys/module/n1x_npu_bootstrap/srcversion').read_text().strip(),
                  transport_before=before,automatic_retry=False,learned_inference='physical N1X DLA via original NVIDIA cuDLA')
    record('attempt.json',metadata)
    print(f'DLA super-resolution: {width}x{height} -> {width*3}x{height*3}, {len(plan)} tiles',flush=True)
    started=time.monotonic();completed=0
    try:
        with Device() as device:
            network=device.load(path)
            def infer(data):
                nonlocal completed
                result=network(data)
                completed+=1
                if completed%10==0 or completed==len(plan):print(f'DLA tiles: {completed}/{len(plan)}',flush=True)
                return result
            planes=tiled_planes(luminance,infer,plan=plan)
        elapsed=time.monotonic()-started
    except Exception as exc:
        record('error.json',dict(error=str(exc),completed_tiles=completed,submission_state_uncertain=True,automatic_retry=False))
        raise
    with Transport('/dev/accel/accel0') as transport:after=transport.info()
    if after['retained_jobs']!=before['retained_jobs']+1 or after['retained_bytes']!=before['retained_bytes']+required:
        raise RuntimeError('kernel backing grew unexpectedly; stopped')
    # Save the physical result before computing any reference inference.
    output=assemble_rgb(planes,cb,cr)
    save_image('upscaled.png',output);save_image('input.png',rgb)
    save_image('bicubic.png',rgb.resize(output.size,Image.Resampling.BICUBIC))
    np.save(destination/'dla-planes.npy',planes);owned(destination/'dla-planes.npy')
    reference_started=time.monotonic()
    reference=CpuReference(ROOT/'sources/demo-research/super-resolution-10.onnx')(luminance[None,None])
    difference=planes.astype(np.float64)-reference
    maximum=float(np.max(np.abs(difference)));rmse=float(np.sqrt(np.mean(difference*difference)))
    passed=maximum<=0.01 and rmse<=0.001
    metadata.update(dla_pipeline_seconds=elapsed,reference_seconds=time.monotonic()-reference_started,
                    maximum_absolute_error=maximum,rmse=rmse,reference_passed=passed,
                    acceptance=dict(max_abs=0.01,rmse=0.001),transport_after=after,
                    output_sha256=hashlib.sha256((destination/'upscaled.png').read_bytes()).hexdigest())
    record('result.json',metadata)
    title='N1X DLA Super-Resolution'
    page=f'''<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>body{{background:#111;color:#eee;font:16px system-ui;margin:32px}}main{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}img{{width:100%;height:auto}}pre{{white-space:pre-wrap}}a{{color:#9cf}}</style>
<h1>{title}</h1><p>{width}×{height} → {width*3}×{height*3}; real DLA learned inference.</p>
<main><section><h2>Input</h2><img src="input.png"></section><section><h2>Bicubic</h2><img src="bicubic.png"></section><section><h2>DLA 3×</h2><a href="upscaled.png"><img src="upscaled.png"></a></section></main>
<p>CPU: color conversion, pixel shuffle, chroma resizing. Independent CPU ONNX reference: {'PASS' if passed else 'FAIL'}.</p>
<pre>{html.escape(json.dumps(metadata,indent=2))}</pre>'''
    (destination/'report.html').write_text(page);owned(destination/'report.html')
    if not passed:raise RuntimeError('DLA result differs from the independent reference; evidence retained')
    print(f'PASS: full DLA output saved to {destination/"upscaled.png"}; max error {maximum:.6g}, RMSE {rmse:.6g}',flush=True)


if __name__=='__main__':main()
