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
import struct
from inspect_dla_compiler import NATIVE_HASH

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=('t234','t254','t256'),help='Attempt an offline compile for this explicit candidate target')
    parser.add_argument('--model',choices=('relu','conv1x1','conv1x1_relu'),default='relu',help='Graph fixture to construct')
    parser.add_argument('--capture-mtl',action='store_true',help='Stop under GDB and save intermediate MTL buffers without translation')
    parser.add_argument('--capture-translation-input',action='store_true',
                        help='Replay verified Windows pretranslation outputs and capture the next frontend handoff')
    parser.add_argument('--package-windows-output',action='store_true',
                        help='Package the exact verified Windows backend outputs with the native frontend')
    args=parser.parse_args()
    if args.capture_mtl and not args.target:parser.error('--capture-mtl requires --target')
    if args.capture_translation_input and (args.target!='t254' or args.capture_mtl):
        parser.error('--capture-translation-input requires --target t254 and excludes --capture-mtl')
    if args.package_windows_output and (args.target!='t254' or args.capture_mtl or args.capture_translation_input):
        parser.error('--package-windows-output requires --target t254 and excludes capture modes')
    suffix='' if args.model=='relu' else '-'+args.model
    outdir=ROOT/'results'/('native-compile-'+args.target+suffix) if args.target else None
    if os.environ.get('N1X_COMPILER_SANDBOX')!='1':
        command=['bwrap','--unshare-all','--die-with-parent','--clearenv',
                 '--ro-bind','/usr','/usr','--symlink','usr/lib','/lib',
                 '--symlink','usr/lib','/lib64','--proc','/proc','--dev','/dev',
                 '--ro-bind',sys.base_prefix,sys.base_prefix,
                 '--ro-bind',str(Path(sys.base_prefix).resolve()),str(Path(sys.base_prefix).resolve()),
                 '--tmpfs','/tmp','--ro-bind',str(ROOT),str(ROOT),'--chdir',str(ROOT),
                 '--setenv','N1X_COMPILER_SANDBOX','1',
                 '--setenv','N1X_COMPILER_MODEL',args.model]
        if outdir:
            outdir.mkdir(parents=True,exist_ok=True)
            command+=['--bind',str(outdir),str(outdir)]
        if args.capture_mtl or args.capture_translation_input or args.package_windows_output:
            gdb_script=('package_windows_output.gdb' if args.package_windows_output else
                        'capture_native_translate.gdb' if args.capture_translation_input else 'capture_native_mtl.gdb')
            command+=['--setenv','N1X_MTL_CAPTURE_DIR',str(outdir),'gdb','-batch',
                      '-x',str(ROOT/'scripts'/gdb_script),'--args']
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
    class Dims2(C.Structure):_fields_=[('h',C.c_int32),('w',C.c_int32)]
    class Weights(C.Structure):_fields_=[('dtype',C.c_uint8),('values',H),('count',C.c_int64)]
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
        if args.model=='relu':
            invoke('NvDlaNetworkAddActivation',[H,H,C.c_uint8,PH],network,tensor,0,C.byref(layer))
        else:
            # C wrapper 0x633434: five Dims2 values, two indirect 24-byte
            # weight structs, bias-mode byte, group count, and output handle.
            assert C.sizeof(Weights)==24 and Weights.values.offset==8 and Weights.count.offset==16
            kernel_values=[(i%3+1)/4 if j==i else -0.125 if j==(i+1)%32 else 0
                           for i in range(32) for j in range(32)]
            bias_values=[i/32-0.5 for i in range(32)]
            kernel_data=C.create_string_buffer(b''.join(struct.pack('<e',v) for v in kernel_values))
            bias_data=C.create_string_buffer(b''.join(struct.pack('<e',v) for v in bias_values))
            kernel=Weights(2,C.cast(kernel_data,H),1024)
            bias=Weights(2,C.cast(bias_data,H),32)
            invoke('NvDlaNetworkAddConvolution',
                   [H,H,C.c_int32,C.c_int32,Dims2,Dims2,Dims2,Dims2,Dims2,Weights,Weights,C.c_uint8,C.c_int32,PH],
                   network,tensor,32,0,Dims2(1,1),Dims2(0,0),Dims2(0,0),Dims2(1,1),Dims2(1,1),
                   kernel,bias,2,1,C.byref(layer))
            for name,expected,values in (('NvDlaLayerGetKernelWeights',kernel,kernel_values),
                                         ('NvDlaLayerGetBiasWeights',bias,bias_values)):
                got=Weights()
                invoke(name,[H,C.POINTER(Weights)],layer,C.byref(got))
                # The original constructor converts FP16 constants to FP32.
                assert got.dtype==1 and got.count==expected.count,(name,got.dtype,got.count)
                assert struct.unpack('<%df'%got.count,C.string_at(got.values,got.count*4))==tuple(values)
        assert layer.value
        invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output));assert output.value
        intermediate=None
        if args.model=='conv1x1_relu':
            intermediate=H(output.value)
            invoke('NvDlaNetworkAddActivation',[H,H,C.c_uint8,PH],network,intermediate,0,C.byref(layer))
            invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output));assert output.value
            assert output.value!=intermediate.value
            linked=H()
            invoke('NvDlaLayerGetInput',[H,C.c_int32,PH],layer,0,C.byref(linked))
            assert linked.value==intermediate.value
        layer_count=C.c_uint32()
        invoke('NvDlaNetworkGetNumLayers',[H,C.POINTER(C.c_uint32)],network,C.byref(layer_count))
        assert layer_count.value==(2 if args.model=='conv1x1_relu' else 1)
        invoke('NvDlaNetworkMarkOutput',[H,H],network,output)
        invoke('NvDlaTensorSetName',[H,C.c_char_p],output,b'output')
        # Numeric definitions from the public NvDlaType.h counterpart:
        # ACTIVATION_TYPE_RELU=0, DATA_TYPE_HALF=2. Native getters must agree
        # with the values supplied; this is still not executed inference.
        if args.model in ('relu','conv1x1_relu'):
            activation=C.c_uint8(255)
            invoke('NvDlaActivationLayerGetActivationType',[H,C.POINTER(C.c_uint8)],layer,C.byref(activation))
            assert activation.value==0
        for item in (tensor,output)+((intermediate,) if intermediate else ()):
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
            (outdir/(args.model+'.nvda')).write_bytes(data)
            compiled=dict(target=args.target,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
                          backend='verified Windows result replay' if args.package_windows_output else 'native',
                          hardware_access=False,inference_verified=False)
            (outdir/'manifest.json').write_text(json.dumps(compiled,indent=2)+'\n')
        print(json.dumps(dict(passed=True,hardware_access=False,sandbox='private /dev, absent /sys, isolated namespaces',
                             native_library_loaded=True,graph_constructed=True,dimension_fields=[dims.a,dims.b,dims.c,dims.d],
                             model=args.model,activation_code=0 if args.model in ('relu','conv1x1_relu') else None,
                             data_type_code=2,calls=calls,compiled=compiled,
                             limitations=['Enum names use public reference definitions; no numerical execution validates them here',
                                          'A named compiler target is not proof of physical N1X or firmware compatibility',
                                          'No numerical inference or device submission']),indent=2))
    finally:
        # Native destructor is void, unlike the status-returning creation API.
        destroy=lib.NvDlaDestroyNetwork;destroy.argtypes=[H];destroy.restype=None;destroy(network)


if __name__=='__main__':main()
