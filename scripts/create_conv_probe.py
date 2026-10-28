#!/usr/bin/env python3
"""Create independent ONNX/CPU references for the compiled convolution fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import onnx
from onnx import helper, numpy_helper, TensorProto
from onnx.reference import ReferenceEvaluator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
from run_conv import inputs, expected_output, load_model, SHAPE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--relu', action='store_true', help='check the compiled Conv->ReLU graph')
    args = parser.parse_args()
    # Also verify source graph coefficients against actual loadable constants.
    name='conv1x1_relu' if args.relu else 'conv1x1'
    if args.output is None:
        args.output=ROOT/('results/conv-relu-probe' if args.relu else 'results/conv-probe')
    compiled=load_model(ROOT/f'results/native-compile-t254-{name}/{name}.nvda')
    kernel = np.zeros((32, 32, 1, 1), dtype=np.float16)
    for c in range(32):
        kernel[c, c, 0, 0] = (c%3+1)/4
        kernel[c, (c+1)%32, 0, 0] = -0.125
    bias = (np.arange(32)/32-0.5).astype(np.float16)
    nodes=[helper.make_node('Conv', ['input', 'kernel', 'bias'], ['intermediate' if args.relu else 'output'],
                          kernel_shape=[1, 1], strides=[1, 1], dilations=[1, 1],
                          pads=[0, 0, 0, 0], group=1)]
    if args.relu:nodes.append(helper.make_node('Relu',['intermediate'],['output']))
    graph = helper.make_graph(nodes, 'n1x_'+name+'_probe',
        [helper.make_tensor_value_info('input', TensorProto.FLOAT16, SHAPE)],
        [helper.make_tensor_value_info('output', TensorProto.FLOAT16, SHAPE)],
        [numpy_helper.from_array(kernel, 'kernel'), numpy_helper.from_array(bias, 'bias')])
    model = helper.make_model(graph, producer_name='n1x-npu-research',
                              opset_imports=[helper.make_opsetid('', 13)], ir_version=8)
    onnx.checker.check_model(model, full_check=True)
    evaluator = ReferenceEvaluator(model)
    files = {name+'-fp16.onnx': model.SerializeToString()}
    cases = []
    for name, packed in inputs().items():
        # Independent vectorized CHW16 unpack/pack, not run_conv.index().
        tensor = np.frombuffer(packed, dtype='<f2').reshape(1, 2, 7, 7, 16).transpose(0, 1, 4, 2, 3).reshape(SHAPE)
        actual = evaluator.run(None, {'input': tensor})[0]
        assert actual.shape == SHAPE and actual.dtype == np.float16
        expected = expected_output(packed,relu=args.relu)
        result = np.ascontiguousarray(actual.reshape(1, 2, 16, 7, 7).transpose(0, 1, 3, 4, 2), dtype='<f2').tobytes()
        assert result == expected, name
        assert np.all(np.isfinite(actual)) and np.any(actual > 0)
        if args.relu:assert np.all(actual>=0) and np.any(actual==0)
        else:assert np.all(actual!=0) and np.any(actual<0)
        for suffix, data in (('input.fp16', packed), ('expected.fp16', expected)):
            files[f'{name}-{suffix}'] = data
        cases.append(dict(name=name, reference_match=True, elements=actual.size,
                          minimum=float(actual.min()), maximum=float(actual.max())))
    assert len({files[f'{c["name"]}-expected.fp16'] for c in cases}) == len(cases)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (args.output/name).write_bytes(data)
    manifest = dict(loadable_sha256=compiled.sha256, shape=SHAPE, dtype='FP16',
                    packing='N,C/16,H,W,16; little endian',
                    reference='ONNX '+('Conv->ReLU' if args.relu else 'Conv')+' ReferenceEvaluator matches exact integer arithmetic in units of 1/128',
                    numpy_version=np.__version__, onnx_version=onnx.__version__,
                    hardware_execution=False, cases=cases,
                    files={name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                           for name, data in files.items()})
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
