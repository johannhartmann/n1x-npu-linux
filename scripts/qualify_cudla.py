#!/usr/bin/env python3
"""Qualify the real cuDLA -> Linux driver -> firmware path, without retries.

Eight numerical jobs with independent reference files. Firmware startup is
manual and must already have passed the separate startup qualifier.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from loadable import Model
from n1x_dla import Device
from run_conv import inputs, expected_output, load_model
from qualify_p16_start import KERNEL, fields
RELU_LOADABLE_SHA256='561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--device',default='/dev/accel/accel0')
    parser.add_argument('--prepare-only',action='store_true',help='create inputs/references; no CUDA or device access')
    args=parser.parse_args()
    root=args.output_dir.resolve();root.mkdir(exist_ok=False)
    owner=int(os.environ['PKEXEC_UID']) if os.geteuid()==0 and 'PKEXEC_UID' in os.environ else None
    def owned(path):
        if owner is not None:os.chown(path,owner,-1)
    owned(root)
    def save(path,data):
        with path.open('xb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        owned(path)
    def record(name,**row):save(root/name,(json.dumps(row,indent=2)+'\n').encode())
    import struct
    jobs=[]
    for variant in ('relu','conv1x1','conv1x1_relu'):
        directory='native-compile-t254'+('' if variant=='relu' else '-'+variant)
        path=ROOT/'results'/directory/(variant+'.nvda')
        model=Model.from_file(path) if variant=='relu' else load_model(path)
        if variant=='relu':
            if model.sha256!=RELU_LOADABLE_SHA256:raise RuntimeError('ReLU loadable hash mismatch')
            patterns={str(sign):struct.pack('<1568e',*(sign*(-1 if i%2 else 1)*(0.5+(i%31)/8) for i in range(1568)))
                      for sign in (-1,1)}
        else:patterns=inputs()
        for name,data in patterns.items():
            if variant=='relu':reference=struct.pack('<1568e',*(max(0,x) for x in struct.unpack('<1568e',data)))
            else:reference=expected_output(data,relu=variant=='conv1x1_relu')
            prefix=f'{len(jobs):03d}-{variant}-{name}'
            input_path=root/(prefix+'.input.fp16');reference_path=root/(prefix+'.expected.fp16')
            save(input_path,data);save(reference_path,reference)
            jobs.append(dict(model=str(path),model_sha256=model.sha256,input=str(input_path),
                             expected=str(reference_path),output=str(root/(prefix+'.output.fp16')),
                             input_sha256=hashlib.sha256(data).hexdigest(),
                             expected_sha256=hashlib.sha256(reference).hexdigest(),
                             retained_bytes=(len(model.image)+8192+4095)&~4095))
    package=ROOT/'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive'
    library=package/'lib/libcudla.so.1.0.0'
    expected_library='89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9'
    if hashlib.sha256(library.read_bytes()).hexdigest()!=expected_library:raise RuntimeError('cuDLA frontend mismatch')
    executable=ROOT/'runtime/cudla/cudla-smoke'
    backend=ROOT/'runtime/cudla/libnvcudla.so'
    record('plan.json',jobs=jobs,frontend_sha256=expected_library,
           backend_sha256=hashlib.sha256(backend.read_bytes()).hexdigest(),
           application_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
           prepared_only=args.prepare_only,hardware_verified=False)
    if args.prepare_only:
        print(f'Prepared {len(jobs)} jobs; no device access; {sum(j["retained_bytes"] for j in jobs)} retained bytes required')
        return
    if os.geteuid()!=0 or platform.release()!=KERNEL:raise RuntimeError('requires root and audited npu2 kernel')
    profile=Path('/sys/firmware/acpi/platform_profile').read_text().strip()
    built=subprocess.check_output(['modinfo','-F','srcversion',str(ROOT/'driver/n1x_npu_bootstrap.ko')],text=True).strip()
    if Path('/sys/module/n1x_npu_bootstrap/srcversion').read_text().strip()!=built:raise RuntimeError('loaded module differs from reviewed build')
    endpoint=Path('/sys/bus/platform/devices/NVDA200A:00')
    status=fields((endpoint/'status').read_text());hfrp=fields((endpoint/'hfrp_status').read_text())
    if status.get('task_verified')!='1' or status.get('lpwr_accepted')!='1' or hfrp.get('power_accepted')!='1':
        raise RuntimeError('startup/bypass qualification required')
    with Device(args.device) as device:info=device.info()
    required=sum(j['retained_bytes'] for j in jobs)
    if required>info['retain_limit']-info['retained_bytes'] or len(jobs)>info['max_jobs']-info['retained_jobs']:
        raise RuntimeError('insufficient retained allocation budget; no jobs submitted')
    record('preflight.json',boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
           module_srcversion=built,platform_profile=profile,info=info,status=status,hfrp=hfrp)
    env={k:v for k,v in os.environ.items() if not k.startswith(('LD_','NVTX_','N1X_','TEST_'))}
    env.update(LD_LIBRARY_PATH=str(ROOT/'runtime/cudla'),N1X_DLA_DEVICE=args.device)
    for index,job in enumerate(jobs):
        record(f'{index:03d}.attempt.json',**job,automatic_retry=False)
        try:
            result=subprocess.run([str(executable),job['model'],job['input'],job['expected'],job['output']],
                                  env=env,capture_output=True,timeout=45)
        except subprocess.TimeoutExpired as exc:
            record(f'{index:03d}.error.json',error=str(exc),submission_state_uncertain=True,automatic_retry=False)
            raise
        save(root/f'{index:03d}.stdout',result.stdout);save(root/f'{index:03d}.stderr',result.stderr)
        output=Path(job['output'])
        if output.exists():owned(output)
        if result.returncode:
            record(f'{index:03d}.error.json',returncode=result.returncode,submission_state_uncertain=True,automatic_retry=False)
            raise RuntimeError(f'cuDLA job {index} failed; stopped without retry')
        digest=hashlib.sha256(output.read_bytes()).hexdigest()
        if digest!=job['expected_sha256']:raise RuntimeError('output/reference mismatch; stopped')
        record(f'{index:03d}.result.json',output_sha256=digest,numerical_match=True,returncode=0)
        print(f'cuDLA job {index}: independently verified output',flush=True)
    record('complete.json',numerical_jobs=len(jobs),physical_cudla_path_verified=True,
           scope='three pinned graphs, changing inputs, blocking CUDA staging; no recovery/lifetime qualification')


if __name__=='__main__':main()
