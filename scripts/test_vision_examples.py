#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Independent CPU checks for trained graphs, partitioning and image boundaries; no NPU."""
import json
from pathlib import Path
import sys
import numpy as np
import onnx
from onnx import helper as h, numpy_helper as nh, TensorProto as T
import onnxruntime as ort
import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from vision_image import im2col, maxpool, denoise, resnet18, classify_input, reference, psnr, ssim
from superres_image import fixed_tiles


def partition_executor(quantized=False):
    """Explicit CPU simulation, not a fallback used by the physical demos."""
    graphs = json.loads((ROOT/'sources/vision/graphs.json').read_text())
    weights = np.load(ROOT/'sources/vision/weights.npz',allow_pickle=False)
    sessions = {}
    for name,desc in graphs.items():
        nodes = []; initializers = []; current = 'input'
        for item in desc['layers']:
            prefix = item['name']; inputs = [current]
            for suffix in ('.weight','.bias'):
                value = weights[prefix+suffix]
                if quantized and suffix=='.weight': value=value.astype(np.float16).astype(np.float32)
                initializers.append(nh.from_array(value,prefix+suffix)); inputs.append(prefix+suffix)
            out = prefix+'.conv'
            nodes.append(h.make_node('Conv',inputs,[out],pads=[item['padding']]*4,strides=[item['stride']]*2))
            if item['relu']:
                nodes.append(h.make_node('Relu',[out],[prefix+'.relu'])); out=prefix+'.relu'
            current = out
        shape = list(desc['input']); output=list(desc['output'])
        if name.startswith('denoise-'): shape[-2:]=['height','width']; output[-2:]=['height','width']
        model = h.make_model(h.make_graph(nodes,name,[h.make_tensor_value_info('input',T.FLOAT,shape)],
            [h.make_tensor_value_info(current,T.FLOAT,output)],initializers),opset_imports=[h.make_opsetid('',13)],ir_version=8)
        onnx.checker.check_model(model)
        opts=ort.SessionOptions(); opts.intra_op_num_threads=4
        sessions[name]=ort.InferenceSession(model.SerializeToString(),sess_options=opts,providers=['CPUExecutionProvider'])
    def infer(name,data):
        value = np.ascontiguousarray(data,dtype=np.float32)
        if quantized: value=value.astype(np.float16).astype(np.float32)
        result=sessions[name].run(None,{'input':value})[0]
        return result.astype(np.float16).astype(np.float32) if quantized else result
    return infer


def torch_resnet(data):
    state = torch.load(ROOT/'sources/vision/resnet18-f37072fd.pth',map_location='cpu',weights_only=True)
    def conv_bn(x,name,bn,stride=1):
        w=state[name+'.weight']; padding=w.shape[2]//2
        x=F.conv2d(x,w,stride=stride,padding=padding)
        return F.batch_norm(x,state[bn+'.running_mean'],state[bn+'.running_var'],
                            state[bn+'.weight'],state[bn+'.bias'],training=False,eps=1e-5)
    with torch.no_grad():
        x=F.max_pool2d(torch.relu(conv_bn(torch.from_numpy(data),'conv1','bn1',2)),3,2,1)
        for i in range(8):
            name=f'layer{i//2+1}.{i%2}'; stride=2 if i in (2,4,6) else 1
            residual=x
            y=conv_bn(torch.relu(conv_bn(x,name+'.conv1',name+'.bn1',stride)),name+'.conv2',name+'.bn2')
            if stride==2: residual=conv_bn(x,name+'.downsample.0',name+'.downsample.1',2)
            x=torch.relu(y+residual)
        return F.linear(x.mean((-2,-1)),state['fc.weight'],state['fc.bias']).numpy()


def main():
    torch.set_num_threads(4)
    rng=np.random.default_rng(53)
    for channels,kernel,padding,stride in ((3,7,3,2),(64,3,1,2),(128,1,0,2)):
        x=rng.normal(size=(1,channels,17,20)).astype(np.float32)
        w=rng.normal(size=(4,channels,kernel,kernel)).astype(np.float32)
        expected=F.conv2d(torch.from_numpy(x),torch.from_numpy(w),padding=padding,stride=stride).numpy()
        gathered=im2col(x,kernel,padding,stride)
        actual=np.einsum('oc,nchw->nohw',w.reshape(4,-1),gathered)
        assert np.allclose(actual,expected,atol=2e-4,rtol=2e-4)
    x=rng.normal(size=(1,5,112,112)).astype(np.float32)
    assert np.array_equal(maxpool(x),F.max_pool2d(torch.from_numpy(x),3,2,1).numpy())
    infer=partition_executor()
    for height,width in ((96,96),(97,193),(193,219)):
        x=rng.normal(.4,.15,(height,width)).astype(np.float32)
        coverage=np.zeros((height,width),dtype=np.uint8)
        for tile in fixed_tiles(height,width,96,halo=17):
            coverage[tile.y:tile.y+tile.height,tile.x:tile.x+tile.width]+=1
        assert np.all(coverage==1)
        _,noise,_=denoise(x,infer)
        expected=reference('dncnn-noise.onnx',x[None,None])[0,0]
        error=float(np.max(np.abs(noise-expected)))
        assert np.allclose(noise,expected,atol=2e-5,rtol=2e-4),(height,width,error)
        print(json.dumps(dict(test='denoise seams/corners',shape=[height,width],max_abs=error,npu_execution=False)),flush=True)
    _,data=classify_input(ROOT/'demo/assets/astronaut-input.png')
    cpu=reference('resnet18.onnx',data)
    pytorch=torch_resnet(data)
    assert np.allclose(cpu,pytorch,atol=1e-4,rtol=1e-4),float(np.max(np.abs(cpu-pytorch)))
    with np.load(ROOT/'sources/vision/weights.npz',allow_pickle=False) as weights:
        output=resnet18(data,infer,weights)
        assert np.allclose(cpu,output,atol=2e-4,rtol=2e-4),float(np.max(np.abs(cpu-output)))
        half=resnet18(data,partition_executor(quantized=True),weights)
        difference=half.astype(np.float64)-cpu
        maximum=float(np.max(np.abs(difference))); rmse=float(np.sqrt(np.mean(difference*difference)))
        assert maximum<.25 and rmse<.05 and half.argmax()==cpu.argmax()
    assert abs(ssim(data[0,0],data[0,0])-1)<1e-6
    # Compare our separable SSIM filter with an independent 2D convolution.
    a=rng.random((45,49)); b=np.clip(a+rng.normal(0,.05,a.shape),0,1)
    axis=np.arange(11)-5; g=np.exp(-(axis*axis)/(2*1.5**2)); g/=g.sum()
    kernel=torch.from_numpy((g[:,None]*g[None,:])[None,None])
    def filtered(x): return F.conv2d(torch.from_numpy(x[None,None]),kernel).numpy()[0,0]
    ma,mb=filtered(a),filtered(b)
    va,vb,cov=filtered(a*a)-ma*ma,filtered(b*b)-mb*mb,filtered(a*b)-ma*mb
    expected_ssim=np.mean(((2*ma*mb+.01**2)*(2*cov+.03**2))/((ma*ma+mb*mb+.01**2)*(va+vb+.03**2)))
    assert abs(ssim(a,b)-expected_ssim)<1e-10
    print(json.dumps(dict(test='ResNet ONNX vs original PyTorch vs partitioned FP32 vs FP16 simulation',
                         top1=int(cpu.argmax()),fp16_max_abs=maximum,fp16_rmse=rmse,npu_execution=False)),flush=True)
    print('PASS: CPU graph, original checkpoint, im2col/pooling, partition and image boundary checks; no NPU execution')


if __name__=='__main__': main()
