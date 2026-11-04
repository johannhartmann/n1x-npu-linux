#!/usr/bin/env python3
"""Qualify the cuDLA -> Linux driver -> N1X firmware path across 8 numerical jobs."""
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
from loadable import Model, require
from n1x_compiler import ensure_reference_loadable
from n1x_dla import Device
from qualify_p16_start import KERNEL, fields
RELU_LOADABLE_SHA256='561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942'
LOADABLE_HASH='717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b'
CONV_RELU_HASH='fe57704f93edbafe9e374e14675cfc34cc5a44858497c6126dec56a60a837536'
SHAPE=(1,32,7,7)
BYTES=3136


def _index(c,h,w):
    return ((c//16*7+h)*7+w)*16+c%16


def inputs():
    import struct
    cases={}
    for name,coefficients in (('channels',(13,7,3)),('spatial',(17,5,11))):
        values=[0]*1568
        a,b,d=coefficients
        for c in range(32):
            for h in range(7):
                for w in range(7):
                    values[_index(c,h,w)]=(2*((a*c+b*h+d*w)%32)-31)/16
        cases[name]=struct.pack('<1568e',*values)
    cases['negative_channels']=struct.pack('<1568e',*(
        -v for v in struct.unpack('<1568e',cases['channels'])))
    return cases


def expected_output(data,relu=False):
    import struct
    require(len(data)==BYTES,'input must contain 1568 packed FP16 values')
    values=struct.unpack('<1568e',data)
    allowed={n/16 for n in range(-31,32,2)}
    require(all(v in allowed for v in values),'input must use odd multiples of 1/16 in [-31/16,31/16]')
    numerators=[int(v*16) for v in values]
    result=[0]*1568
    for c in range(32):
        for h in range(7):
            for w in range(7):
                at=_index(c,h,w)
                n=2*(c%3+1)*numerators[at]-numerators[_index((c+1)%32,h,w)]+4*c-64
                assert n!=0
                result[at]=max(0,n)/128 if relu else n/128
    encoded=struct.pack('<1568e',*result)
    assert tuple(result)==struct.unpack('<1568e',encoded)
    return encoded


def load_model(path):
    import struct
    model=Model.from_file(path)
    require(model.sha256 in (LOADABLE_HASH,CONV_RELU_HASH),'model differs from checked convolution artifacts')
    require(len(model.inputs)==len(model.outputs)==1,'unexpected I/O count')
    for tensor in (*model.inputs,*model.outputs):
        require(tensor.shape==SHAPE and tensor.bytes==BYTES and
                tensor.format_codes==(3,2,2,36,0) and
                tensor.strides==(2,224,1568,0,3136,0,0,0),'unexpected tensor layout')
    start,size=model.ranges[2]
    require(size==8192,'unexpected constant allocation')
    kernel=struct.unpack('<1024e',model.image[start:start+2048])
    require(kernel==tuple((c%3+1)/4 if j==c else -0.125 if j==(c+1)%32 else 0
                          for c in range(32) for j in range(32)),'kernel differs from graph')
    bias_words=struct.unpack('<32I',model.image[start+4096:start+4224])
    expected_bias=tuple(struct.unpack('<I',struct.pack('<f',c/32-0.5))[0]>>13 for c in range(32))
    require(bias_words==expected_bias,'FP19 bias differs from graph')
    return model


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--device',default='/dev/accel/accel0')
    parser.add_argument('--prepare-only',action='store_true',help='create inputs/references; no CUDA or device access')
    parser.add_argument('--reuse',action='store_true',help='keep each graph loaded for changing inputs; require one kernel allocation per graph')
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
        path=ensure_reference_loadable(variant,ROOT)
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
    groups=[[job] for job in jobs] if not args.reuse else [
        [job for job in jobs if job['model']==model] for model in dict.fromkeys(job['model'] for job in jobs)]
    record('plan.json',jobs=jobs,allocations=len(groups),frontend_sha256=expected_library,
           backend_sha256=hashlib.sha256(backend.read_bytes()).hexdigest(),
           application_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
           prepared_only=args.prepare_only,reuse=args.reuse,hardware_verified=False)
    if args.prepare_only:
        print(f'Prepared {len(jobs)} jobs; no device access; {sum(g[0]["retained_bytes"] for g in groups)} retained bytes required')
        return
    if os.geteuid()!=0:raise RuntimeError('requires root')
    profile=Path('/sys/firmware/acpi/platform_profile').read_text().strip()
    built=subprocess.check_output(['modinfo','-F','srcversion',str(ROOT/'driver/n1x_npu_bootstrap.ko')],text=True).strip()
    if Path('/sys/module/n1x_npu_bootstrap/srcversion').read_text().strip()!=built:raise RuntimeError('loaded module differs from reviewed build')
    endpoint=Path('/sys/bus/platform/devices/NVDA200A:00')
    status=fields((endpoint/'status').read_text())
    hfrp=fields((endpoint/'hfrp_status').read_text()) if (endpoint/'hfrp_status').exists() else {}
    if status.get('task_verified')!='1' or status.get('lpwr_accepted')!='1' or (hfrp and hfrp.get('power_accepted')!='1'):
        raise RuntimeError('startup/bypass qualification required')
    with Device(args.device) as device:info=device.info()
    if args.reuse and not info['capabilities']&4:
        raise RuntimeError('completed-task reuse capability absent; no jobs submitted')
    required=sum(group[0]['retained_bytes'] for group in groups)
    if required>info['retain_limit']-info['retained_bytes'] or len(groups)>info['max_jobs']-info['retained_jobs']:
        raise RuntimeError('insufficient retained allocation budget; no jobs submitted')
    record('preflight.json',boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
           module_srcversion=built,platform_profile=profile,info=info,status=status,hfrp=hfrp)
    env={k:v for k,v in os.environ.items() if not k.startswith(('LD_','NVTX_','N1X_','TEST_'))}
    env.update(LD_LIBRARY_PATH=str(ROOT/'runtime/cudla'),N1X_DLA_DEVICE=args.device)
    for index,group in enumerate(groups):
        record(f'{index:03d}.attempt.json',jobs=group,automatic_retry=False,reuse=args.reuse)
        with Device(args.device) as device:before=device.info()
        argv=[str(executable),group[0]['model']]
        for job in group:argv.extend([job['input'],job['expected'],job['output']])
        try:
            result=subprocess.run(argv,env=env,capture_output=True,timeout=45*len(group))
        except subprocess.TimeoutExpired as exc:
            record(f'{index:03d}.error.json',error=str(exc),submission_state_uncertain=True,automatic_retry=False)
            raise
        save(root/f'{index:03d}.stdout',result.stdout);save(root/f'{index:03d}.stderr',result.stderr)
        for job in group:
            output=Path(job['output'])
            if output.exists():owned(output)
        if result.returncode:
            record(f'{index:03d}.error.json',returncode=result.returncode,submission_state_uncertain=True,automatic_retry=False)
            raise RuntimeError(f'cuDLA group {index} failed; stopped without retry')
        digests=[]
        for job in group:
            digest=hashlib.sha256(Path(job['output']).read_bytes()).hexdigest()
            if digest!=job['expected_sha256']:raise RuntimeError('output/reference mismatch; stopped')
            digests.append(digest)
        with Device(args.device) as device:after=device.info()
        if args.reuse and (after['retained_jobs']!=before['retained_jobs']+1 or
                           after['retained_bytes']!=before['retained_bytes']+group[0]['retained_bytes']):
            raise RuntimeError('changing-input reuse allocated unexpected kernel backing; stopped')
        record(f'{index:03d}.result.json',output_sha256=digests,numerical_match=True,returncode=0,
               info_before=before,info_after=after,reuse_submissions=len(group)-1 if args.reuse else 0)
        print(f'cuDLA group {index}: {len(group)} independently verified outputs',flush=True)
    record('complete.json',numerical_jobs=len(jobs),physical_cudla_path_verified=True,
           io_reuse_verified=args.reuse,reuse_submissions=len(jobs)-len(groups),
           scope='three pinned graphs, changing inputs, blocking CUDA staging; no recovery/lifetime qualification')


if __name__=='__main__':main()
