#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Acquire hash-pinned trained models and prepare bounded CNN partitions, offline.

torch.load uses weights_only=True. No vendor code, CUDA or NPU execution.
The independent ONNX references retain the source ResNet BatchNormalization.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import onnx
from onnx import helper as h, numpy_helper as nh, TensorProto as T

ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    ('dncnn_25.pth', 'https://github.com/cszn/KAIR/releases/download/v1.0/dncnn_25.pth',
     '0451a70de9b672ae037270498fbb1c17a1c1c4403785df586ff65df5b858e5b0'),
    ('resnet18-f37072fd.pth', 'https://download.pytorch.org/models/resnet18-f37072fd.pth',
     'f37072fd47e89c5e827621c5baffa7500819f7896bbacec160b1a16c560e07ec'),
    ('imagenet_classes.txt', 'https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt',
     '1f386e0d1cb6e28b9c2dac651c3dea6801e98ad1b41a14ce6bb1a093d72069f5'),
    ('dog.jpg', 'https://raw.githubusercontent.com/pytorch/hub/master/images/dog.jpg',
     'f3f87bb8ab3c26c7ecfd3ac60421d7f32b0503d1d6c5baf8bac42ed93d86351a'),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(fetch=False):
    from prepare_inputs import download, verified
    folder = ROOT/'sources/vision'
    folder.mkdir(parents=True, exist_ok=True)
    for name, url, sha in ASSETS:
        if fetch: download('sources/vision/'+name, url, sha)
        if not verified(folder/name, sha):
            raise ValueError('missing '+name+'; rerun with --fetch')
    import torch
    def checkpoint(name):
        return {k: v.detach().cpu().numpy().copy() for k,v in torch.load(
            folder/name, map_location='cpu', weights_only=True).items()}
    dn = checkpoint('dncnn_25.pth'); rn = checkpoint('resnet18-f37072fd.pth')
    weights = {}; graphs = {}; nodes = []; initializers = []
    def init(name, value):
        initializers.append(nh.from_array(np.asarray(value, dtype=np.float32), name)); return name
    def node(op, inputs, output, **attrs):
        nodes.append(h.make_node(op, inputs, [output], **attrs)); return output
    def save_reference(name, output, channels, size):
        graph = h.make_graph(nodes, name, [h.make_tensor_value_info('input', T.FLOAT, [1,channels,*size])],
                             [h.make_tensor_value_info(output, T.FLOAT, None)], initializers)
        model = h.make_model(graph, opset_imports=[h.make_opsetid('', 13)], ir_version=8)
        model = onnx.shape_inference.infer_shapes(model)
        onnx.checker.check_model(model); onnx.save(model, folder/name)
    layers = []; current = 'input'
    for i in range(17):
        name = f'dncnn.{i}'
        kernel, bias = dn[f'model.{i*2}.weight'], dn[f'model.{i*2}.bias']
        assert kernel.shape == (64 if i<16 else 1, 1 if i==0 else 64, 3, 3)
        weights[name+'.weight'], weights[name+'.bias'] = kernel, bias
        layers.append(dict(name=name, padding=1, stride=1, relu=i<16))
        current = node('Conv', [current, init(name+'.weight', kernel), init(name+'.bias', bias)], name,
                       pads=[1]*4, strides=[1,1])
        if i<16: current = node('Relu', [current], name+'.relu')
    save_reference('dncnn-noise.onnx', current, 1, ['height', 'width'])
    for part, (start, end) in enumerate(((0,6),(6,12),(12,17))):
        subset = layers[start:end]
        graphs[f'denoise-{part}'] = dict(input=[1,1 if start==0 else 64,96,96],
                                       output=[1,1 if end==17 else 64,96,96], layers=subset,
                                       operation_count=sum(2+item['relu'] for item in subset), halo=17)
    nodes = []; initializers = []
    def conv_bn(source, name, bn, stride=1, relu=False):
        kernel = rn[name+'.weight']; channels = kernel.shape[0]
        gamma, beta = rn[bn+'.weight'], rn[bn+'.bias']
        mean, variance = rn[bn+'.running_mean'], rn[bn+'.running_var']
        factor = gamma/np.sqrt(variance+np.float32(1e-5))
        weights[name+'.weight'] = kernel*factor[:,None,None,None]
        weights[name+'.bias'] = beta-mean*factor
        padding = kernel.shape[2]//2
        out = node('Conv', [source, init(name+'.original', kernel)], name+'.raw',
                   pads=[padding]*4, strides=[stride,stride])
        out = node('BatchNormalization', [out, init(bn+'.gamma', gamma), init(bn+'.beta', beta),
                   init(bn+'.mean', mean), init(bn+'.variance', variance)], name+'.bn', epsilon=1e-5)
        if relu: out = node('Relu', [out], name+'.relu')
        return out, dict(name=name, padding=padding, stride=stride, relu=relu)
    current, stem = conv_bn('input', 'conv1', 'bn1', 2, True)
    # Explicit host im2col avoids the recovered frontend's implicit stride-2
    # lowering (which introduces extra backend operations and constants).
    # This is an exact permutation/gather, not host learned computation.
    weights['conv1.weight'] = weights['conv1.weight'].reshape(64,147,1,1)
    stem.update(padding=0, stride=1)
    # The pointwise equivalent is independent at every spatial location.
    # Reuse one 28x28 NPU allocation over the full 112x112 feature map.
    graphs['resnet18-stem'] = dict(input=[1,147,28,28], output=[1,64,28,28], layers=[stem], operation_count=3,
                                 im2col=dict(kernel=7, padding=3, stride=2))
    current = node('MaxPool', [current], 'stem.pool', kernel_shape=[3,3], strides=[2,2], pads=[1]*4)
    for i in range(8):
        stage, block = i//2+1, i%2
        name = f'layer{stage}.{block}'; channels = 64*2**(stage-1)
        stride = 2 if i in (2,4,6) else 1
        side = 56//2**(stage-1); input_side = side*stride
        inputs = channels//2 if stride==2 else channels
        residual = current
        first, l1 = conv_bn(current, name+'.conv1', name+'.bn1', stride, True)
        second, l2 = conv_bn(first, name+'.conv2', name+'.bn2')
        if stride==2:
            weights[name+'.conv1.weight'] = weights[name+'.conv1.weight'].reshape(channels,inputs*9,1,1)
            l1.update(padding=0, stride=1)
        graphs[f'resnet18-b{i}'] = dict(input=[1,inputs,input_side,input_side], output=[1,channels,side,side],
                                      layers=[l1,l2], operation_count=5)
        if stride==2:
            graphs[f'resnet18-b{i}'].update(input=[1,inputs*9,side,side], im2col=dict(kernel=3,padding=1,stride=2))
        if stride==2:
            residual, down = conv_bn(residual, name+'.downsample.0', name+'.downsample.1', 2)
            down.update(stride=1)
            graphs[f'resnet18-down{i}'] = dict(input=[1,inputs,side,side], output=[1,channels,side,side],
                                            layers=[down], operation_count=2)
        current = node('Relu', [node('Add', [second,residual], name+'.add')], name+'.out')
    current = node('Flatten', [node('GlobalAveragePool', [current], 'pool')], 'flat', axis=1)
    current = node('Gemm', [current, init('fc.weight', rn['fc.weight']), init('fc.bias', rn['fc.bias'])], 'logits', transB=1)
    weights['fc.weight'], weights['fc.bias'] = rn['fc.weight'], rn['fc.bias']
    save_reference('resnet18.onnx', current, 3, [224,224])
    # np.savez emits stable ZIP metadata; all inputs are numeric arrays, no pickle.
    np.savez(folder/'weights.npz', **weights)
    sha = digest(folder/'weights.npz')
    for desc in graphs.values(): desc['weights_sha256'] = sha
    (folder/'graphs.json').write_text(json.dumps(graphs, indent=2)+'\n')
    receipt = dict(sources={name:sha for name,_,sha in ASSETS},
                   outputs={name:digest(folder/name) for name in ('weights.npz','graphs.json','dncnn-noise.onnx','resnet18.onnx')},
                   hardware_access=False)
    (folder/'prepared.json').write_text(json.dumps(receipt, indent=2)+'\n')
    manifest = ROOT/'config/vision-models.json'
    if manifest.is_file():
        expected = json.loads(manifest.read_text())['prepared']
        for name,sha in expected.items():
            if digest(folder/name)!=sha: raise ValueError('prepared artifact differs from pinned manifest: '+name)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true')
    prepare(parser.parse_args().fetch)
