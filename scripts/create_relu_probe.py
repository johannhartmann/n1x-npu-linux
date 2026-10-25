#!/usr/bin/env python3
"""Generate a fixed FP16 ReLU ONNX probe and a CPU-verified numerical oracle.

No TensorRT builder, CUDA, firmware, MMIO or NPU execution is invoked.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import onnx
from onnx import helper,TensorProto
from onnx.reference import ReferenceEvaluator


def chw16(tensor):
    n,c,h,w=tensor.shape
    assert c%16==0
    return np.ascontiguousarray(tensor.reshape(n,c//16,16,h,w).transpose(0,1,3,4,2),dtype='<f2')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('results/relu-probe'))
    args=parser.parse_args()
    shape=(1,32,7,7)
    graph=helper.make_graph([helper.make_node('Relu',['input'],['output'])],'n1x_relu_probe',
                            [helper.make_tensor_value_info('input',TensorProto.FLOAT16,shape)],
                            [helper.make_tensor_value_info('output',TensorProto.FLOAT16,shape)])
    model=helper.make_model(graph,producer_name='n1x-npu-research',
                            opset_imports=[helper.make_opsetid('',13)],ir_version=8)
    onnx.checker.check_model(model,full_check=True)
    # Exactly representable, finite, normal binary fractions and +0; no
    # subnormal/NaN/signed-zero policy is hidden in the reference comparison.
    inputs=((np.arange(np.prod(shape),dtype=np.int32)%33-16)/8).astype(np.float16).reshape(shape)
    expected=np.maximum(inputs,np.float16(0))
    actual=ReferenceEvaluator(model).run(None,{'input':inputs})[0]
    assert actual.dtype==np.float16 and actual.tobytes()==expected.tobytes()
    assert np.any(inputs<0) and np.any(inputs>0) and np.any(inputs==0)
    packed=chw16(inputs)
    unpacked=packed.transpose(0,1,4,2,3).reshape(shape)
    assert unpacked.tobytes()==inputs.tobytes()
    args.output.mkdir(parents=True,exist_ok=True)
    payloads={'relu-fp16.onnx':model.SerializeToString(),
              'input-nchw-fp16.bin':inputs.astype('<f2').tobytes(),
              'expected-nchw-fp16.bin':expected.astype('<f2').tobytes(),
              'input-chw16-fp16.bin':packed.tobytes(),
              'expected-chw16-fp16.bin':chw16(expected).tobytes()}
    manifest=dict(shape=list(shape),operation='Relu',dtype='float16',opset=13,onnx_ir_version=8,
                  reference='ONNX ReferenceEvaluator compared byte-for-byte with independent NumPy maximum',
                  onnx_version=onnx.__version__,numpy_version=np.__version__,
                  io_packing='N,C/16,H,W,16; little-endian FP16; C=32 needs no channel padding',
                  hardware_execution=False,compiled_loadable=False,files={})
    for name,data in payloads.items():
        (args.output/name).write_bytes(data)
        manifest['files'][name]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':main()
