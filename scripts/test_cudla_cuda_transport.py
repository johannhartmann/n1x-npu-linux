#!/usr/bin/env python3
"""Real NVIDIA cuDLA frontend + our backend + real CUDA, modeled NPU transport.

The fake kernel returns a fixed byte pattern, never computes a model. This
qualifies CUDA staging, model serialization and error handling, NOT inference.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from loadable import Model

MOCK=r'''
#include "n1x_dla.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
static unsigned char *saved_image;
static unsigned int saved_bytes;
static unsigned long long saved_id;
static int fail(const char *name) {const char *p=getenv("TEST_FAILURE");return p&&!strcmp(p,name);}
static void event(const char *name) {
 FILE *f=fopen(getenv("TEST_EVENTS"),"a");assert(f);assert(fprintf(f,"%s\n",name)>0);assert(!fclose(f));
}
int n1x_dla_open(const char *p) {(void)p;event("open");return fail("open")?-EACCES:open("/dev/null",O_RDWR);}
int n1x_dla_info(int fd,struct n1x_dla_accel_info *i) {
 assert(fd>=0);event("info");memset(i,0,sizeof(*i));i->abi_version=1;
 i->capabilities=getenv("TEST_REUSE")?N1X_DLA_CAP_REEXEC_IO:0;
 i->retain_limit=fail("budget")?1:64U<<20;i->max_jobs=128;return 0;
}
int n1x_dla_submit(int fd,const void *image,unsigned int bytes,
 const struct n1x_dla_accel_range *ranges,unsigned int count,struct n1x_dla_accel_exec *r) {
 assert(fd>=0);event("EXEC");
 free(saved_image);saved_image=malloc(bytes);assert(saved_image);memcpy(saved_image,image,bytes);saved_bytes=bytes;saved_id=7;
 FILE *f=fopen(getenv("TEST_IMAGE"),"wb");assert(f);assert(fwrite(image,1,bytes,f)==bytes);assert(!fclose(f));
 f=fopen(getenv("TEST_RANGES"),"wb");assert(f);assert(fwrite(ranges,sizeof(*ranges),count,f)==count);assert(!fclose(f));
 memset(r,0,sizeof(*r));r->job_id=7;r->completed=!fail("exec");r->status=fail("exec")?-ETIMEDOUT:0;return r->status;
}
int n1x_dla_resubmit(int fd,unsigned long long id,const struct n1x_dla_accel_patch *patches,
 unsigned int count,struct n1x_dla_accel_exec *result) {
 assert(fd>=0&&id==saved_id&&getenv("TEST_REUSE"));event("REEXEC");
 for(unsigned int i=0;i<count;i++){
  assert(patches[i].offset<saved_bytes&&patches[i].bytes<=saved_bytes-patches[i].offset);
  memcpy(saved_image+patches[i].offset,(const void *)(uintptr_t)patches[i].data_ptr,patches[i].bytes);
 }
 FILE *f=fopen(getenv("TEST_IMAGE"),"wb");assert(f);assert(fwrite(saved_image,1,saved_bytes,f)==saved_bytes);assert(!fclose(f));
 memset(result,0,sizeof(*result));result->job_id=++saved_id;
 result->status=fail("exec")?-ETIMEDOUT:0;result->completed=!result->status;return result->status;
}
int n1x_dla_read(int fd,unsigned long long job,unsigned long long offset,void *out,unsigned long long bytes) {
 assert(fd>=0&&job==saved_id);event("READ");
 assert(offset==strtoull(getenv("TEST_OUTPUT_OFFSET"),NULL,0));
 assert(bytes==strtoull(getenv("TEST_OUTPUT_BYTES"),NULL,0));
 if(fail("read"))return -EIO;
 memset(out,0x37,bytes);return 0;
}
'''

CUDA_FAULTS=r'''
#define _GNU_SOURCE
#include <cuda.h>
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int fail(const char *name) {const char *p=getenv("TEST_FAILURE");return p&&!strcmp(p,name);}
static void event(const char *name) {
 FILE *f=fopen(getenv("TEST_EVENTS"),"a");assert(f);fprintf(f,"%s\n",name);fclose(f);
}
CUresult cuMemcpyDtoHAsync(void *dst,CUdeviceptr src,size_t n,CUstream stream) {
 if(fail("cuda-input-copy")){event("input-copy-failed");return CUDA_ERROR_INVALID_VALUE;}
 CUresult (*next)(void*,CUdeviceptr,size_t,CUstream)=dlsym(RTLD_NEXT,"cuMemcpyDtoHAsync_v2");assert(next);
 return next(dst,src,n,stream);
}
CUresult cuMemcpyHtoDAsync(CUdeviceptr dst,const void *src,size_t n,CUstream stream) {
 if(fail("cuda-output-copy")){event("output-copy-failed");return CUDA_ERROR_INVALID_VALUE;}
 CUresult (*next)(CUdeviceptr,const void*,size_t,CUstream)=dlsym(RTLD_NEXT,"cuMemcpyHtoDAsync_v2");assert(next);
 return next(dst,src,n,stream);
}
CUresult cuStreamSynchronize(CUstream stream) {
 static unsigned int calls;
 CUresult (*next)(CUstream)=dlsym(RTLD_NEXT,"cuStreamSynchronize");assert(next);
 CUresult status=next(stream);calls++;
 // Real copies are drained first. Only the reported result is injected;
 // this is not a hardware CUDA fault or proof of behavior after one.
 if((calls==1&&fail("cuda-input-sync"))||(calls==2&&fail("cuda-output-sync"))) {
  event("reported-sync-error");return CUDA_ERROR_LAUNCH_FAILED;
 }
 return status;
}
CUresult cuMemFreeHost(void *p) {
 event("free-host");CUresult (*next)(void*)=dlsym(RTLD_NEXT,"cuMemFreeHost");assert(next);return next(p);
}
'''


def main():
    subprocess.run(['make','cudla'],cwd=ROOT,check=True,stdout=sys.stderr)
    cases=[]
    with tempfile.TemporaryDirectory(prefix='n1x-cudla-cuda-') as tmp:
        directory=Path(tmp)
        source=directory/'transport.c'; source.write_text(MOCK)
        subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror',
                        '-I'+str(ROOT/'include'),'-I'+str(ROOT/'runtime'),str(source),
                        '-Wl,-soname,libn1x_dla.so','-o',str(directory/'libn1x_dla.so')],check=True)
        fault_source=directory/'faults.c';fault_source.write_text(CUDA_FAULTS)
        fault_library=directory/'faults.so'
        subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-I/opt/cuda/include',
                        str(fault_source),'-ldl','-o',str(fault_library)],check=True)
        for variant in ('relu','conv1x1','conv1x1_relu'):
            subdir='native-compile-t254'+('' if variant=='relu' else '-'+variant)
            path=ROOT/'results'/subdir/(variant+'.nvda')
            model=Model.from_file(path)
            data=bytes((i*13+7)%256 for i in range(model.inputs[0].bytes))
            image,ranges=model.prepare({model.inputs[0].name:data})
            image[model.outputs[0].offset:model.outputs[0].offset+model.outputs[0].bytes]=b'\xff'*model.outputs[0].bytes
            for failure in ('', 'open','budget','exec','read','wrong-reference',
                            'cuda-input-copy','cuda-output-copy','cuda-input-sync','cuda-output-sync'):
                case=directory/(variant+'-'+(failure or 'success'));case.mkdir()
                (case/'input').write_bytes(data)
                (case/'expected').write_bytes(bytes([0x38 if failure=='wrong-reference' else 0x37])*model.outputs[0].bytes)
                env={k:v for k,v in os.environ.items() if not k.startswith(('LD_','NVTX_','N1X_','TEST_'))}
                env.update(LD_LIBRARY_PATH=str(directory)+':'+str(ROOT/'runtime/cudla'),
                           LD_PRELOAD=str(fault_library),
                           N1X_DLA_DEVICE='/dev/modeled-npu-only',TEST_FAILURE=failure,
                           TEST_EVENTS=str(case/'events'),TEST_IMAGE=str(case/'image'),
                           TEST_RANGES=str(case/'ranges'),TEST_OUTPUT_OFFSET=str(model.outputs[0].offset),
                           TEST_OUTPUT_BYTES=str(model.outputs[0].bytes))
                result=subprocess.run([str(ROOT/'runtime/cudla/cudla-smoke'),str(path),str(case/'input'),
                                       str(case/'expected'),str(case/'output')],env=env,capture_output=True,text=True,timeout=30)
                assert result.returncode==(1 if failure else 0),(variant,failure,result.stdout,result.stderr)
                events=(case/'events').read_text().splitlines()
                submitted=failure not in ('open','budget','cuda-input-copy','cuda-input-sync')
                assert events.count('EXEC')==int(submitted),events
                if submitted:
                    assert (case/'image').read_bytes()==image
                    assert (case/'ranges').read_bytes()==b''.join(struct.pack('<QQ',*r) for r in ranges)
                if not failure or failure=='wrong-reference':
                    assert (case/'output').read_bytes()==bytes([0x37])*model.outputs[0].bytes
                else:assert not (case/'output').read_bytes()
                if failure in ('cuda-input-sync','cuda-output-sync'):
                    assert 'reported-sync-error' in events and 'free-host' not in events,events
                elif failure not in ('open','budget'):
                    assert events.count('free-host')==1,events
                cases.append(dict(model=variant,failure=failure or None,events=events,exit_code=result.returncode))
        # Exercise the production numerical application's same-module sequence,
        # with distinct inputs; this peer returns a fixed pattern, not inference.
        for failure in ('', 'wrong-reference'):
            case=directory/('smoke-changing-inputs-'+(failure or 'success'));case.mkdir()
            argv=[str(ROOT/'runtime/cudla/cudla-smoke'),str(path)]
            patterns=[bytes((i*13+7+step*41)%256 for i in range(model.inputs[0].bytes)) for step in range(3)]
            for step,data in enumerate(patterns):
                inp,ref,out=(case/f'{step}.{name}' for name in ('input','expected','output'))
                inp.write_bytes(data)
                ref.write_bytes(bytes([0x38 if failure and step==1 else 0x37])*model.outputs[0].bytes)
                argv.extend([str(inp),str(ref),str(out)])
            env.update(TEST_EVENTS=str(case/'events'),TEST_IMAGE=str(case/'image'),TEST_RANGES=str(case/'ranges'),
                       TEST_FAILURE='',TEST_REUSE='1')
            result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
            assert result.returncode==(1 if failure else 0),(result.stdout,result.stderr)
            events=(case/'events').read_text().splitlines()
            assert events.count('EXEC')==1 and events.count('REEXEC')==(1 if failure else 2),events
            last=1 if failure else 2
            image,_=model.prepare({model.inputs[0].name:patterns[last]})
            image[model.outputs[0].offset:model.outputs[0].offset+model.outputs[0].bytes]=b'\xff'*model.outputs[0].bytes
            assert (case/'image').read_bytes()==image
            for step in range(3):
                assert (case/f'{step}.output').read_bytes()==(b'' if failure and step==2 else bytes([0x37])*model.outputs[0].bytes)
            cases.append(dict(model='production changing-input reuse',failure=failure or None,events=events))
        env.pop('TEST_REUSE',None)
        package=ROOT/'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive'
        api=directory/'api'
        subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',
                        '-I'+str(package/'include'),'-I/opt/cuda/include',str(ROOT/'scripts/test_cudla_api.cpp'),
                        '-L'+str(package/'lib'),'-lcudla','-L/opt/cuda/lib64/stubs','-lcuda',
                        '-Wl,-rpath,'+str(package/'lib'),'-o',str(api)],check=True)
        case=directory/'api-case';case.mkdir()
        env.update(TEST_EVENTS=str(case/'events'),TEST_IMAGE=str(case/'image'),TEST_RANGES=str(case/'ranges'),TEST_FAILURE='')
        result=subprocess.run([str(api),str(path)],env=env,capture_output=True,text=True,timeout=30)
        assert result.returncode==0,(result.stdout,result.stderr)
        events=(case/'events').read_text().splitlines()
        assert events.count('EXEC')==1 and 'READ' not in events,events
        cases.append(dict(model='API regression',events=events,exit_code=0))
        case=directory/'reuse-case';case.mkdir()
        env.update(TEST_EVENTS=str(case/'events'),TEST_IMAGE=str(case/'image'),TEST_RANGES=str(case/'ranges'),
                   TEST_FAILURE='',TEST_REUSE='1')
        result=subprocess.run([str(api),str(path),'repeat'],env=env,capture_output=True,text=True,timeout=30)
        assert result.returncode==0,(result.stdout,result.stderr)
        events=(case/'events').read_text().splitlines()
        assert events.count('EXEC')==1 and events.count('REEXEC')==64 and events.count('READ')==64,events
        cases.append(dict(model='64 completed I/O reuse cycles and sticky error',events=events,exit_code=0))
    print(json.dumps(dict(real_nvidia_frontend=True,real_cuda=True,npu_transport='modeled',
                         numerical_inference_test=False,cases=cases),indent=2))


if __name__=='__main__':main()
