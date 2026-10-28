#!/usr/bin/env python3
"""Single physical cuDLA execution of the compiled super-resolution graph.

Stores raw NPU output and independent CPU-reference metrics, never a preview.
Stops on any execution failure without retry.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from cudla_python import Device
from superres_image import CpuReference,open_luminance
from n1x_dla import Device as Transport
from loadable import Model


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('requires CAP_SYS_RAWIO/root')
    profile=Path('/sys/firmware/acpi/platform_profile').read_text().strip()
    destination=args.output_dir.resolve();destination.mkdir(exist_ok=False)
    owner=int(os.environ['PKEXEC_UID']) if 'PKEXEC_UID' in os.environ else None
    if owner is not None:os.chown(destination,owner,-1)
    def save(name,data):
        with (destination/name).open('xb') as file:
            file.write(data);file.flush();os.fsync(file.fileno())
            if owner is not None:os.fchown(file.fileno(),owner,-1)
    def record(name,data):save(name,(json.dumps(data,indent=2)+'\n').encode())
    path=ROOT/'results/native-compile-t254-superres/superres.nvda'
    materialized=Model.from_file(path)
    with Transport('/dev/accel/accel0') as transport:before=transport.info()
    required=(len(materialized.image)+8192+4095)&~4095
    if required>before['retain_limit']-before['retained_bytes']:raise RuntimeError('insufficient retained memory; no submission')
    _,source,_,_=open_luminance(ROOT/'sources/demo-research/astronaut.png')
    data=source[192:256,192:256][None,None].astype(np.float16).astype(np.float32)
    reference=CpuReference(ROOT/'sources/demo-research/super-resolution-10.onnx')(data)
    save('input-nchw.fp16',data.astype('<f2').tobytes());save('reference-nchw.fp32',reference.astype('<f4').tobytes())
    record('attempt.json',dict(model_sha256=materialized.sha256,model_image_bytes=len(materialized.image),
                              input_sha256=hashlib.sha256(data.astype('<f2').tobytes()).hexdigest(),
                              frontend='original NVIDIA cuDLA',platform_profile=profile,transport_before=before,automatic_retry=False))
    started=time.monotonic()
    with Device() as device:
        module=device.load(path)
        result=module(data)
    elapsed=time.monotonic()-started
    save('npu-output-nchw.fp32',result.astype('<f4').tobytes())
    with Transport('/dev/accel/accel0') as transport:after=transport.info()
    delta=result-reference
    metrics=dict(shape=list(result.shape),seconds=elapsed,maximum_absolute_error=float(np.max(np.abs(delta))),
                 rmse=float(np.sqrt(np.mean(delta.astype(np.float64)**2))),
                 reference_min=float(reference.min()),reference_max=float(reference.max()),
                 output_min=float(result.min()),output_max=float(result.max()),
                 finite=bool(np.isfinite(result).all()),transport_after=after,
                 physical_execution=after['retained_jobs']==before['retained_jobs']+1)
    record('result.json',metrics)
    print(json.dumps(metrics,indent=2),flush=True)


if __name__=='__main__':main()
