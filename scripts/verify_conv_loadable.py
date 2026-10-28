#!/usr/bin/env python3
"""Verify the convolution artifact and identify the native loader's next gap."""
import hashlib
import json
import struct
import sys

from inspect_dla_compiler import ROOT
from inspect_loadable import Buffer, inspect

LOADABLE_HASH='717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b'


def main():
    directory=ROOT/'results/native-compile-t254-conv1x1'
    data=(directory/'conv1x1.nvda').read_bytes()
    assert len(data)==2817056 and hashlib.sha256(data).hexdigest()==LOADABLE_HASH
    manifest=json.loads((directory/'manifest.json').read_text())
    assert manifest['sha256']==LOADABLE_HASH and manifest['bytes']==len(data)
    assert manifest['backend']=='verified Windows result replay' and not manifest['inference_verified']
    replay=json.loads((directory/'packaging-replay.json').read_text())
    backend=ROOT/'results/windows-conv1x1-backend'
    assert replay['passed'] and replay['backend_manifest_sha256']==hashlib.sha256((backend/'manifest.json').read_bytes()).hexdigest()
    final=json.loads((backend/'manifest.json').read_text())
    assert final['passed'] and final['translation']['return_value']==0
    projected=inspect(data);view=Buffer(data);root=view.u('<I',0)
    assert projected['version_bytes']==[0,9,0]
    assert projected['root_vector_lengths']=={'1':1,'2':10,'3':10,'4':1,'5':10,'6':2,'7':0,'8':1}
    assert projected['tasks']==[dict(id=0,interface=1,instance=-1,address_ids=[4,2,1,3,5,6,7,8,9],pre_actions=[],post_actions=[0])]
    symbols={'task-0-nw':3,'task-0-inst':0,'task-0-ctrl_data':1,'task-0-preempt_ctx':4,'task-0-stats':5}
    constants={'tb-1':'constant-258.bin','tb-2':'constant-8960.bin'}
    properties={'property_global_dram_pool_size':4224,'property_local_cvsram_pool_size':0,'property_local_dram_pool_size':0}
    blobs={}
    for table in view.tables(root,5):
        name=view.string(table,0);extent=view.vector(table,5,1);payload=data[extent.start:extent.stop]
        assert view.scalar(table,1,'<Q')==len(payload) and name not in blobs
        blobs[name]=payload
        if name in symbols:expected=(backend/f'kind-{symbols[name]}.bin').read_bytes()
        elif name in constants:expected=(directory/constants[name]).read_bytes()
        else:expected=struct.pack('<Q',properties[name])
        assert payload==expected,name
    assert set(blobs)==set(symbols)|set(constants)|set(properties)
    weights=projected['memory'][1]
    assert weights==dict(id=1,domain=0,flags=3,size=8192,alignment=4096,content_count=2,
                        contents=['tb-1','tb-2'],content_offsets=[0,4096])
    assert projected['addresses'][1]==dict(id=1,memory_id=1,offset=0,size=8192)
    for table in view.tables(root,6):
        assert tuple(view.scalar(table,i,'<i') for i in range(10,14))==(1,32,7,7)
        assert view.scalar(table,3,'<Q')==3136
        assert tuple(view.scalar(table,i,'<B') for i in range(5,10))==(3,2,2,36,0)
    debug=json.loads((backend/'kind-7.bin').read_bytes().rstrip(b'\0'))
    assert len(debug['layers'])==4
    for layer in debug['layers']:
        tensors=layer['input_tensors']
        assert len(tensors)==3
        assert [(t['storage']['address_list_index'],t['storage']['src_offset_bytes']) for t in tensors[1:]]==[(2,0),(2,4096)]
        assert [t['dtype'] for t in tensors[1:]]==['DATA_TYPE_FP16','DATA_TYPE_FP19']
    sys.path.insert(0,str(ROOT/'runtime'))
    from loadable import Model,LoadableError
    try:Model(data)
    except LoadableError as exc:
        loader_gap=str(exc)
        assert loader_gap=='nonzero or unknown memory-pool property'
    else:raise AssertionError('Update the qualification test when global-pool support is implemented')
    print(json.dumps({'passed':True,'hardware_access':False,'loadable_sha256':LOADABLE_HASH,
                      'bytes':len(data),'backend_instructions':len(blobs['task-0-inst']),
                      'constant_bytes':[len(blobs[n]) for n in constants],
                      'global_dram_property':4224,'constant_allocation_bytes':8192,
                      'native_loader_supported':False,'native_loader_gap':loader_gap,
                      'limitations':['Exact original-compiler fixture, not arbitrary ONNX compilation',
                                      'Compiler describes FP19 bias storage; physical interpretation and numerical results unverified',
                                      'Nonzero global DRAM pool needs original-runtime investigation before enabling native loading',
                                      'No physical DLA submission or inference']},indent=2))


if __name__=='__main__':main()
