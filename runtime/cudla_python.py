"""Python application binding to the original NVIDIA cuDLA frontend.

Explicit hybrid CUDA execution only. Reference inference lives elsewhere.
"""
import ctypes as c
import hashlib
import os
from pathlib import Path
import numpy as np

from n1x_dla import Device as _DlaDevice

ROOT=Path(__file__).resolve().parents[1]
FRONTEND_SHA256='89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9'


class Descriptor(c.Structure):
    _fields_=[('name',c.c_char*81),('size',c.c_uint64),('n',c.c_uint64),
              ('channels',c.c_uint64),('h',c.c_uint64),('w',c.c_uint64),
              ('codes',c.c_uint8*5),('strides',c.c_uint32*8)]
    @property
    def shape(self):return self.n,self.channels,self.h,self.w


def _descriptor(tensor):
    value=Descriptor()
    value.name=tensor.name.encode()
    value.size=tensor.bytes
    value.n,value.channels,value.h,value.w=tensor.shape
    for index,item in enumerate(tensor.format_codes):value.codes[index]=item
    for index,item in enumerate(tensor.strides):value.strides[index]=item
    return value


def describe_model(model):
    def _fmt(desc):
        return {
            'name':desc.name.decode('utf-8',errors='replace'),
            'size':int(desc.size),
            'shape':list(desc.shape),
            'dataFormat':int(desc.codes[0]),
            'dataType':int(desc.codes[1]),
            'dataCategory':int(desc.codes[2]),
            'pixelFormat':int(desc.codes[3]),
            'pixelMapping':int(desc.codes[4]),
            'stride':[int(x) for x in desc.strides],
        }
    inputs=[_descriptor(t) for t in model.inputs]
    outputs=[_descriptor(t) for t in model.outputs]
    return {
        'inputs':[_fmt(d) for d in inputs],
        'outputs':[_fmt(d) for d in outputs],
        'total_input_bytes':sum(d.size for d in inputs),
        'total_output_bytes':sum(d.size for d in outputs),
    }


class Attribute(c.Union):
    _fields_=[('count',c.c_uint32),('descriptor',c.POINTER(Descriptor))]


class Task(c.Structure):
    _fields_=[('module',c.c_void_p),('outputs',c.POINTER(c.c_void_p)),
              ('num_outputs',c.c_uint32),('num_inputs',c.c_uint32),
              ('inputs',c.POINTER(c.c_void_p)),('wait',c.c_void_p),('signal',c.c_void_p)]


def tensor_view(backing,descriptor):
    n,channels,h,w=descriptor.shape
    element,line,surface,_,batch,*_=descriptor.strides
    fmt,dtype,_,pixel_fmt,*_=descriptor.codes
    if n!=1 or min(channels,h,w)<=0:
        raise ValueError('unsupported tensor layout')
    if fmt==3 and dtype==2 and element==2:
        group_ch,np_dtype=16,'<f2'
    elif fmt in (3,5) and dtype==4 and element==1:
        group_ch,np_dtype=(32 if (fmt==5 or pixel_fmt==36) else 16),'<i1'
    else:
        raise ValueError('unsupported tensor layout')
    pixel_group_bytes=group_ch*element
    # Bound the padded-channel view independently of NumPy's stride machinery.
    groups=(channels+group_ch-1)//group_ch
    required=(groups-1)*surface+(h-1)*line+(w-1)*pixel_group_bytes+pixel_group_bytes
    if required>descriptor.size or len(backing)!=descriptor.size or line<w*pixel_group_bytes or surface<h*line:
        raise ValueError('tensor strides outside allocation')
    return np.ndarray((groups,h,w,group_ch),dtype=np_dtype,buffer=backing,strides=(surface,line,pixel_group_bytes,element))


def pack_tensor(data,descriptor):
    dtype='<i1' if descriptor.codes[1]==4 else '<f2'
    source=np.asarray(data,dtype=dtype)
    if source.shape!=descriptor.shape or (dtype=='<f2' and not np.isfinite(source).all()):raise ValueError('invalid input tensor')
    result=bytearray(descriptor.size);view=tensor_view(result,descriptor)
    group_ch=view.shape[3]
    for channel in range(descriptor.channels):view[channel//group_ch,:,:,channel%group_ch]=source[0,channel]
    return result


def unpack_tensor(data,descriptor):
    view=tensor_view(bytearray(data),descriptor)
    group_ch=view.shape[3]
    result=np.empty(descriptor.shape,dtype=view.dtype)
    for channel in range(descriptor.channels):result[0,channel]=view[channel//group_ch,:,:,channel%group_ch]
    if view.dtype==np.dtype('<f2'):
        if not np.isfinite(result).all():raise ValueError('nonfinite DLA result')
        return result.astype(np.float32)
    return result


def bind(library,name,args):
    fn=getattr(library,name);fn.argtypes=args;fn.restype=c.c_int;return fn


def check(status,operation):
    if status:raise RuntimeError(f'{operation} failed: status {status}; no automatic retry')


class Device:
    def __init__(self,models=None,device_path=None):
        self.device_path=device_path or os.environ.get('N1X_DLA_DEVICE','/dev/accel/accel0')
        if models is None:
            self.model_paths={}
        elif isinstance(models,dict):
            self.model_paths={k:Path(v).resolve(strict=True) for k,v in models.items()}
        else:
            self.model_paths={Path(p).stem:Path(p).resolve(strict=True) for p in models}
        self.context=c.c_void_p();self.stream=c.c_void_p();self.handle=c.c_void_p();self.modules=[]
        self.cuda=c.CDLL('libcuda.so.1')
        cuda_args={
            'cuInit':[c.c_uint], 'cuDevicePrimaryCtxRetain':[c.POINTER(c.c_void_p),c.c_int],
            'cuDevicePrimaryCtxRelease_v2':[c.c_int], 'cuCtxSetCurrent':[c.c_void_p],
            'cuCtxSynchronize':[], 'cuStreamCreate':[c.POINTER(c.c_void_p),c.c_uint],
            'cuStreamDestroy_v2':[c.c_void_p], 'cuMemAlloc_v2':[c.POINTER(c.c_uint64),c.c_size_t],
            'cuMemFree_v2':[c.c_uint64], 'cuMemcpyHtoD_v2':[c.c_uint64,c.c_void_p,c.c_size_t],
            'cuMemcpyDtoH_v2':[c.c_void_p,c.c_uint64,c.c_size_t]}
        for name,args in cuda_args.items():bind(self.cuda,name,args)
        frontend=ROOT/'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive/lib/libcudla.so.1.0.0'
        if hashlib.sha256(frontend.read_bytes()).hexdigest()!=FRONTEND_SHA256:raise ValueError('NVIDIA frontend hash mismatch')
        self.backend=c.CDLL(str(ROOT/'runtime/cudla/libnvcudla.so'),mode=c.RTLD_GLOBAL)
        self.dla=c.CDLL(str(frontend))
        args={
            'cudlaCreateDevice':[c.c_uint64,c.POINTER(c.c_void_p),c.c_uint],
            'cudlaDestroyDevice':[c.c_void_p], 'cudlaGetLastError':[c.c_void_p],
            'cudlaModuleLoadFromMemory':[c.c_void_p,c.c_void_p,c.c_size_t,c.POINTER(c.c_void_p),c.c_uint],
            'cudlaModuleGetAttributes':[c.c_void_p,c.c_int,c.POINTER(Attribute)],
            'cudlaModuleUnload':[c.c_void_p,c.c_uint],
            'cudlaMemRegister':[c.c_void_p,c.c_void_p,c.c_size_t,c.POINTER(c.c_void_p),c.c_uint],
            'cudlaMemUnregister':[c.c_void_p,c.c_void_p],
            'cudlaSubmitTask':[c.c_void_p,c.POINTER(Task),c.c_uint,c.c_void_p,c.c_uint]}
        for name,types in args.items():bind(self.dla,name,types)
        try:
            self.call('cuInit',0);self.call('cuDevicePrimaryCtxRetain',c.byref(self.context),0)
            self.call('cuCtxSetCurrent',self.context);self.call('cuStreamCreate',c.byref(self.stream),1)
            self.call('cudlaCreateDevice',0,c.byref(self.handle),0)
        except Exception:self.close();raise

    def call(self,name,*args):
        check(getattr(self.cuda if name.startswith('cu') and not name.startswith('cudla') else self.dla,name)(*args),name)

    def info(self):
        with _DlaDevice(self.device_path) as dla_dev:
            return dla_dev.info()

    def load(self,path_or_name):
        path=self.model_paths.get(str(path_or_name),path_or_name)
        result=Module(self,path);self.modules.append(result);return result

    def close(self):
        for module in self.modules:module.close()
        self.modules.clear()
        if self.handle:self.dla.cudlaDestroyDevice(self.handle);self.handle=c.c_void_p()
        if self.stream:self.cuda.cuStreamDestroy_v2(self.stream);self.stream=c.c_void_p()
        if self.context:self.cuda.cuDevicePrimaryCtxRelease_v2(0);self.context=c.c_void_p()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class Module:
    def __init__(self,device,path):
        self.device=device;self.handle=c.c_void_p();self.in_buffers=[];self.out_buffers=[]
        self.in_tokens=[];self.out_tokens=[];self.inputs=[];self.outputs=[]
        self.failed=False
        raw=Path(path).read_bytes();backing=c.create_string_buffer(raw)
        try:
            device.call('cudlaModuleLoadFromMemory',device.handle,backing,len(raw),c.byref(self.handle),0)
            attr=Attribute()
            device.call('cudlaModuleGetAttributes',self.handle,0,c.byref(attr))
            num_in=int(attr.count)
            device.call('cudlaModuleGetAttributes',self.handle,1,c.byref(attr))
            num_out=int(attr.count)
            if not (1<=num_in<=64 and 1<=num_out<=64):raise ValueError('cuDLA module requires 1..64 inputs and outputs')
            in_arr=(Descriptor*num_in)();attr.descriptor=in_arr
            device.call('cudlaModuleGetAttributes',self.handle,2,c.byref(attr))
            out_arr=(Descriptor*num_out)();attr.descriptor=out_arr
            device.call('cudlaModuleGetAttributes',self.handle,3,c.byref(attr))
            for desc in in_arr:
                self.inputs.append(desc)
                pointer=c.c_uint64();device.call('cuMemAlloc_v2',c.byref(pointer),desc.size)
                self.in_buffers.append(pointer)
                token=c.c_void_p();device.call('cudlaMemRegister',device.handle,c.c_void_p(pointer.value),desc.size,c.byref(token),0)
                self.in_tokens.append(token)
            for desc in out_arr:
                self.outputs.append(desc)
                pointer=c.c_uint64();device.call('cuMemAlloc_v2',c.byref(pointer),desc.size)
                self.out_buffers.append(pointer)
                token=c.c_void_p();device.call('cudlaMemRegister',device.handle,c.c_void_p(pointer.value),desc.size,c.byref(token),0)
                self.out_tokens.append(token)
            self.input,self.output=self.inputs[0],self.outputs[0]
        except Exception:self.close();raise

    def __call__(self,data):
        if self.failed:raise RuntimeError('module stopped after a previous failure')
        device=self.device
        if len(self.inputs)==1 and not isinstance(data,(dict,list,tuple)):
            in_arrays=[data]
        elif isinstance(data,dict):
            in_arrays=[data[d.name.decode('utf-8',errors='replace')] for d in self.inputs]
        else:
            if len(data)!=len(self.inputs):raise ValueError('input tensor count mismatch')
            in_arrays=list(data)
        packed_inputs=[bytes(pack_tensor(arr,desc)) for arr,desc in zip(in_arrays,self.inputs)]
        in_ptrs=(c.c_void_p*len(self.in_tokens))(*[t.value for t in self.in_tokens])
        out_ptrs=(c.c_void_p*len(self.out_tokens))(*[t.value for t in self.out_tokens])
        task=Task(self.handle,out_ptrs,len(self.out_tokens),len(self.in_tokens),in_ptrs,None,None)
        try:
            device.call('cuCtxSetCurrent',device.context)
            for buf,packed in zip(self.in_buffers,packed_inputs):
                src=c.create_string_buffer(packed)
                device.call('cuMemcpyHtoD_v2',buf,src,len(packed))
            device.call('cuCtxSynchronize')
            device.call('cudlaSubmitTask',device.handle,c.byref(task),1,device.stream,0)
            device.call('cudlaGetLastError',device.handle)
            results=[]
            for buf,desc in zip(self.out_buffers,self.outputs):
                dst=c.create_string_buffer(desc.size)
                device.call('cuMemcpyDtoH_v2',dst,buf,desc.size)
                results.append(unpack_tensor(dst.raw,desc))
            return results[0] if len(results)==1 else results
        except Exception:self.failed=True;raise

    def close(self):
        for token in self.in_tokens+self.out_tokens:self.device.dla.cudlaMemUnregister(self.device.handle,token)
        self.in_tokens.clear();self.out_tokens.clear()
        for pointer in self.in_buffers+self.out_buffers:self.device.cuda.cuMemFree_v2(pointer)
        self.in_buffers.clear();self.out_buffers.clear()
        if self.handle:self.device.dla.cudlaModuleUnload(self.handle,0);self.handle=c.c_void_p()
