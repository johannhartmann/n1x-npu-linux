#!/usr/bin/env python3
"""Exercise recovered native graph C API in a device-free bubblewrap sandbox.

With --target, attempt offline compilation using the recovered profile API.
This never executes inference or submits work to hardware. Enum meanings remain
separate from roundtripping numeric values through the recovered C ABI.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import argparse
import signal
import faulthandler
from inspect_dla_compiler import NATIVE_HASH

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=('t234','t254','t256'),help='Attempt an offline compile for this explicit candidate target')
    args=parser.parse_args()
    outdir=ROOT/'results'/('native-compile-'+args.target) if args.target else None
    if os.environ.get('N1X_COMPILER_SANDBOX')!='1':
        command=['bwrap','--unshare-all','--die-with-parent','--clearenv',
                 '--ro-bind','/usr','/usr','--symlink','usr/lib','/lib',
                 '--symlink','usr/lib','/lib64','--proc','/proc','--dev','/dev',
                 '--ro-bind',sys.base_prefix,sys.base_prefix,
                 '--ro-bind',str(Path(sys.base_prefix).resolve()),str(Path(sys.base_prefix).resolve()),
                 '--tmpfs','/tmp','--ro-bind',str(ROOT),str(ROOT),'--chdir',str(ROOT),
                 '--setenv','N1X_COMPILER_SANDBOX','1']
        if outdir:
            outdir.mkdir(parents=True,exist_ok=True)
            command+=['--bind',str(outdir),str(outdir)]
        command+=[str(ROOT/'venv/bin/python'),str(Path(__file__).resolve()),*sys.argv[1:]]
        subprocess.run(command,check=True)
        return
    assert platform.machine()=='aarch64'
    assert not any(Path('/dev').glob('nvidia*')) and not Path('/sys').exists()
    signal.alarm(120)
    faulthandler.enable()
    path=ROOT/'sources/tensorrt-11.4/native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==NATIVE_HASH
    lib=C.CDLL(str(path))
    H=C.c_void_p
    PH=C.POINTER(H)
    class Dims(C.Structure):_fields_=[('a',C.c_int32),('b',C.c_int32),('c',C.c_int32),('d',C.c_int32)]
    def bind(name,args):
        f=getattr(lib,name);f.argtypes=args;f.restype=C.c_uint32;return f
    calls=[]
    def invoke(name,args,*values):
        if outdir:print('CALL '+name,file=sys.stderr,flush=True)
        status=bind(name,args)(*values);calls.append(dict(function=name,status=status))
        if outdir:print('STATUS '+str(status),file=sys.stderr,flush=True)
        assert status==0,(name,status)
    network,tensor,layer,output=H(),H(),H(),H()
    invoke('NvDlaCreateNetwork',[PH],C.byref(network));assert network.value
    try:
        dims=Dims(1,32,7,7)
        invoke('NvDlaNetworkAddInput',[H,C.c_char_p,Dims,PH],network,b'input',dims,C.byref(tensor));assert tensor.value
        observed=Dims()
        invoke('NvDlaTensorGetDimensions',[H,C.POINTER(Dims)],tensor,C.byref(observed))
        assert bytes(observed)==bytes(dims)
        invoke('NvDlaNetworkAddActivation',[H,H,C.c_uint8,PH],network,tensor,0,C.byref(layer));assert layer.value
        invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output));assert output.value
        invoke('NvDlaNetworkMarkOutput',[H,H],network,output)
        invoke('NvDlaTensorSetName',[H,C.c_char_p],output,b'output')
        # Numeric definitions from the public NvDlaType.h counterpart:
        # ACTIVATION_TYPE_RELU=0, DATA_TYPE_HALF=2. Native getters must agree
        # with the values supplied; this is still not executed inference.
        activation=C.c_uint8(255)
        invoke('NvDlaActivationLayerGetActivationType',[H,C.POINTER(C.c_uint8)],layer,C.byref(activation))
        assert activation.value==0
        for item in (tensor,output):
            invoke('NvDlaTensorSetDataType',[H,C.c_uint8],item,2)
            invoke('NvDlaTensorSetDataFormat',[H,C.c_uint8],item,3)
            dtype=C.c_uint8(255)
            invoke('NvDlaTensorGetDataType',[H,C.POINTER(C.c_uint8)],item,C.byref(dtype));assert dtype.value==2
        for name in ('NvDlaNetworkGetNumInputs','NvDlaNetworkGetNumOutputs'):
            count=C.c_uint32(0)
            invoke(name,[H,C.POINTER(C.c_uint32)],network,C.byref(count));assert count.value==1
        invoke('NvDlaTensorGetDimensions',[H,C.POINTER(Dims)],output,C.byref(observed))
        assert bytes(observed)==bytes(dims)
        compiled=None
        if args.target:
            wisdom,compiler,profiler,profile,target,loadable=H(),H(),H(),H(),H(),H()
            invoke('NvDlaCreateWisdom',[PH],C.byref(wisdom));assert wisdom.value
            Path('/tmp/wisdom').mkdir()
            invoke('NvDlaWisdomOpen',[H,C.c_char_p],wisdom,b'/tmp/wisdom')
            invoke('NvDlaWisdomSetDataType',[H,C.c_uint8],wisdom,2)
            invoke('NvDlaWisdomSetNetworkTransient',[H,H],wisdom,network)
            invoke('NvDlaWisdomGetCompiler',[H,PH],wisdom,C.byref(compiler));assert compiler.value
            invoke('NvDlaWisdomGetProfiler',[H,PH],wisdom,C.byref(profiler));assert profiler.value
            invoke('NvDlaProfilerCreateProfile',[H,C.c_char_p,PH],profiler,b'probe',C.byref(profile));assert profile.value
            retrieved=H()
            invoke('NvDlaProfilerGetProfile',[H,C.c_char_p,PH],profiler,b'probe',C.byref(retrieved));assert retrieved.value==profile.value
            invoke('NvDlaProfileInitWithDefaultProfile',[H],profile)
            invoke('NvDlaProfileSetComputePrecision',[H,C.c_uint8],profile,2)
            invoke('NvDlaProfileSetNetworkInputDataFormat',[H,C.c_uint8],profile,3)
            invoke('NvDlaProfileSetNetworkOutputDataFormat',[H,C.c_uint8],profile,3)
            invoke('NvDlaProfilerGetTargetConfig',[H,C.c_char_p,PH],profiler,args.target.encode(),C.byref(target));assert target.value
            name=C.c_char_p()
            invoke('NvDlaTargetConfigGetName',[H,C.POINTER(C.c_char_p)],target,C.byref(name));assert name.value==args.target.encode()
            invoke('NvDlaCompilerCompile',[H,C.c_char_p,C.c_char_p,PH],compiler,b'probe',args.target.encode(),C.byref(loadable));assert loadable.value
            length=C.c_uint64(0)
            invoke('NvDlaCompilerGetLoadableImageSize',[H,C.c_char_p,C.POINTER(C.c_uint64)],compiler,b'probe',C.byref(length))
            assert 0<length.value<64*1024*1024
            image=C.create_string_buffer(length.value)
            invoke('NvDlaCompilerGetLoadableImage',[H,C.c_char_p,H],compiler,b'probe',image)
            data=image.raw
            (outdir/'relu.nvda').write_bytes(data)
            compiled=dict(target=args.target,bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
            (outdir/'manifest.json').write_text(json.dumps(compiled,indent=2)+'\n')
        print(json.dumps(dict(passed=True,hardware_access=False,sandbox='private /dev, absent /sys, isolated namespaces',
                             native_library_loaded=True,graph_constructed=True,dimension_fields=[dims.a,dims.b,dims.c,dims.d],
                             activation_code=0,data_type_code=2,calls=calls,compiled=compiled,
                             limitations=['Enum names use public reference definitions; no numerical execution validates them here',
                                          'A named compiler target is not proof of physical N1X or firmware compatibility',
                                          'No numerical inference or device submission']),indent=2))
    finally:
        # Native destructor is void, unlike the status-returning creation API.
        destroy=lib.NvDlaDestroyNetwork;destroy.argtypes=[H];destroy.restype=None;destroy(network)


if __name__=='__main__':main()
