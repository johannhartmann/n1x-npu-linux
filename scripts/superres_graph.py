#!/usr/bin/env python3
"""Construct the learned ONNX super-resolution graph in the native compiler.

No inference or device access. Pixel shuffle stays outside the learned graph.
"""
import ctypes as C
import hashlib
import json
from pathlib import Path

MODEL_SHA256='85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e'


def build(root,invoke,network,tensor,H,PH,Dims2,Weights,outdir):
    import numpy as np
    import onnx
    from onnx import numpy_helper
    from inspect_fp19_conversion import encode
    path=root/'sources/demo-research/super-resolution-10.onnx'
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MODEL_SHA256:raise ValueError('super-resolution source hash mismatch')
    model=onnx.load_model_from_string(raw)
    onnx.checker.check_model(model)
    weights={t.name:numpy_helper.to_array(t) for t in model.graph.initializer}
    keepalive=[]; tensors=[tensor];spec=[];operations=0
    current=tensor
    for index,(channels,kernel_size) in enumerate(((64,5),(64,3),(32,3),(9,3)),1):
        kernel=np.asarray(weights[f'conv{index}.weight'],dtype='<f4')
        bias=np.asarray(weights[f'conv{index}.bias'],dtype='<f4')
        assert kernel.shape[0]==channels and kernel.shape[2:]==(kernel_size,kernel_size)
        kernel_data=C.create_string_buffer(kernel.tobytes());bias_data=C.create_string_buffer(bias.tobytes())
        keepalive.extend((kernel_data,bias_data))
        layer=H(); output=H(); padding=kernel_size//2
        invoke('NvDlaNetworkAddConvolution',
               [H,H,C.c_int32,C.c_int32,Dims2,Dims2,Dims2,Dims2,Dims2,Weights,Weights,C.c_uint8,C.c_int32,PH],
               network,current,channels,0,Dims2(kernel_size,kernel_size),Dims2(padding,padding),
               Dims2(padding,padding),Dims2(1,1),Dims2(1,1),
               Weights(1,C.cast(kernel_data,H),kernel.size),Weights(1,C.cast(bias_data,H),bias.size),
               2,1,C.byref(layer))
        invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output));assert output.value
        spec.extend((dict(offset=operations*34816+0x258,bytes=int(kernel.size)*2,
                          backing_bytes=((channels+31)//32*32)*((kernel.shape[1]+15)//16*16)*kernel_size*kernel_size*2,
                          source=f'conv{index}.weight',expected_sha256=hashlib.sha256(kernel.astype('<f2').tobytes()).hexdigest()),
                     dict(offset=(operations+1)*34816+0x160,bytes=int(bias.size)*4,
                          backing_bytes=((channels+31)//32*32)*4,
                          source=f'conv{index}.bias',expected_sha256=hashlib.sha256(
                              np.asarray([encode(int(v)) for v in bias.view('<u4')],dtype='<u4').tobytes()).hexdigest())))
        operations+=2;current=output;tensors.append(output)
        if index<4:
            layer=H();output=H()
            invoke('NvDlaNetworkAddActivation',[H,H,C.c_uint8,PH],network,current,0,C.byref(layer))
            invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output));assert output.value
            current=output;tensors.append(output);operations+=1
    if outdir:
        (outdir/'constants-spec.json').write_text(json.dumps(dict(model_sha256=MODEL_SHA256,
              operation_count=operations,entries=spec),indent=2)+'\n')
    return layer,current,tensors,keepalive
