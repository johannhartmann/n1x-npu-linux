#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pinned convolution-only partitions for the two vision examples; no I/O to devices."""
import ctypes as C
import hashlib
import json

MODELS = ('denoise-0', 'denoise-1', 'denoise-2', 'resnet18-stem', *(
    f'resnet18-b{i}' for i in range(8)), *(
    f'resnet18-down{i}' for i in (2, 4, 6)))


def description(root, model):
    if model not in MODELS:
        raise ValueError('unknown vision partition')
    return json.loads((root/'sources/vision/graphs.json').read_text())[model]


def build(root, invoke, network, tensor, H, PH, Dims2, Weights, outdir, model):
    import numpy as np
    from inspect_fp19_conversion import encode
    desc = description(root, model)
    path = root/'sources/vision/weights.npz'
    if hashlib.sha256(path.read_bytes()).hexdigest() != desc['weights_sha256']:
        raise ValueError('prepared vision weights differ from graph manifest')
    tensors = [tensor]; keepalive = []; spec = []; operations = 0
    current = tensor
    with np.load(path, allow_pickle=False) as weights:
        for item in desc['layers']:
            name = item['name']
            kernel = np.ascontiguousarray(weights[name+'.weight'], dtype='<f4')
            bias = np.ascontiguousarray(weights[name+'.bias'], dtype='<f4')
            channels, inputs, kh, kw = kernel.shape
            assert kh == kw and channels == bias.size
            kernel_data = C.create_string_buffer(kernel.tobytes())
            bias_data = C.create_string_buffer(bias.tobytes())
            keepalive.extend((kernel_data, bias_data))
            layer = H(); output = H()
            padding, stride = item['padding'], item['stride']
            invoke('NvDlaNetworkAddConvolution',
                   [H,H,C.c_int32,C.c_int32,Dims2,Dims2,Dims2,Dims2,Dims2,Weights,Weights,C.c_uint8,C.c_int32,PH],
                   network,current,channels,0,Dims2(kh,kw),Dims2(padding,padding),
                   Dims2(padding,padding),Dims2(stride,stride),Dims2(1,1),
                   Weights(1,C.cast(kernel_data,H),kernel.size),
                   Weights(1,C.cast(bias_data,H),bias.size),2,1,C.byref(layer))
            invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output))
            spec.extend((dict(offset=operations*34816+0x258, bytes=int(kernel.size)*2,
                              backing_bytes=((channels+31)//32*32)*((inputs+15)//16*16)*kh*kw*2,
                              expected_sha256=hashlib.sha256(kernel.astype('<f2').tobytes()).hexdigest()),
                         dict(offset=(operations+1)*34816+0x160, bytes=int(bias.size)*4,
                              backing_bytes=((channels+31)//32*32)*4,
                              expected_sha256=hashlib.sha256(np.asarray(
                                  [encode(int(v)) for v in bias.view('<u4')], dtype='<u4').tobytes()).hexdigest())))
            operations += 2; current = output; tensors.append(output)
            if item['relu']:
                layer = H(); output = H()
                invoke('NvDlaNetworkAddActivation',[H,H,C.c_uint8,PH],network,current,0,C.byref(layer))
                invoke('NvDlaLayerGetOutput',[H,C.c_int32,PH],layer,0,C.byref(output))
                operations += 1; current = output; tensors.append(output)
    assert operations == desc['operation_count'] and operations <= 64
    if outdir:
        (outdir/'constants-spec.json').write_text(json.dumps(dict(
            operation_count=operations, entries=spec, graph=desc), indent=2)+'\n')
    return layer, current, tensors, keepalive
