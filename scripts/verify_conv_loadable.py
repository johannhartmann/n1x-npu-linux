#!/usr/bin/env python3
"""Verify the convolution artifact, native materialization and pool validation."""
import hashlib
import json
import struct
import sys
import argparse

from inspect_dla_compiler import ROOT
from inspect_loadable import Buffer, inspect

LOADABLE_HASH='717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b'
CONV_RELU_HASH='fe57704f93edbafe9e374e14675cfc34cc5a44858497c6126dec56a60a837536'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',choices=('conv1x1','conv1x1_relu'),default='conv1x1')
    args=parser.parse_args()
    combined=args.model=='conv1x1_relu'
    digest=CONV_RELU_HASH if combined else LOADABLE_HASH
    directory=ROOT/('results/native-compile-t254-'+args.model)
    data=(directory/(args.model+'.nvda')).read_bytes()
    assert hashlib.sha256(data).hexdigest()==digest
    if not combined:assert len(data)==2817056
    manifest=json.loads((directory/'manifest.json').read_text())
    assert manifest['sha256']==digest and manifest['bytes']==len(data)
    assert manifest['backend']=='verified Windows result replay' and not manifest['inference_verified']
    replay=json.loads((directory/'packaging-replay.json').read_text())
    backend=ROOT/('results/windows-'+args.model+'-backend')
    assert replay['passed'] and replay['backend_manifest_sha256']==hashlib.sha256((backend/'manifest.json').read_bytes()).hexdigest()
    final=json.loads((backend/'manifest.json').read_text())
    assert final['passed'] and final['translation']['return_value']==0
    projected=inspect(data);view=Buffer(data);root=view.u('<I',0)
    assert projected['version_bytes']==[0,9,0]
    assert projected['root_vector_lengths']=={'1':1,'2':11 if combined else 10,'3':11 if combined else 10,'4':1,'5':10,'6':2,'7':0,'8':1}
    ids=[5,3,1,2,4,6,7,8,9,10] if combined else [4,2,1,3,5,6,7,8,9]
    assert projected['tasks']==[dict(id=0,interface=1,instance=-1,address_ids=ids,pre_actions=[],post_actions=[0])]
    symbols={'task-0-nw':3,'task-0-inst':0,'task-0-ctrl_data':1,'task-0-preempt_ctx':4,'task-0-stats':5}
    constants={'tb-1':'constant-258.bin','tb-2':'constant-8960.bin'}
    properties={'property_global_dram_pool_size':4224,'property_local_cvsram_pool_size':0,'property_local_dram_pool_size':3136 if combined else 0}
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
    assert len(debug['layers'])==(8 if combined else 4)
    for layer in debug['layers'][::2] if combined else debug['layers']:
        tensors=layer['input_tensors']
        assert len(tensors)==3
        assert [(t['storage']['address_list_index'],t['storage']['src_offset_bytes']) for t in tensors[1:]]==[(2,0),(2,4096)]
        assert [t['dtype'] for t in tensors[1:]]==['DATA_TYPE_FP16','DATA_TYPE_FP19']
    if combined:
        assert projected['memory'][2]==dict(id=2,domain=0,flags=1,size=4096,alignment=4096,
                                           content_count=0,contents=[],content_offsets=[])
        ends=[]
        for conv,relu in zip(debug['layers'][::2],debug['layers'][1::2]):
            produced=conv['output_tensors'][0];consumed=relu['input_tensors'][0]
            assert len(relu['input_tensors'])==len(relu['output_tensors'])==1
            assert produced['storage']==consumed['storage']
            assert produced['storage']['address_list_index']==3
            assert relu['output_tensors'][0]['storage']['address_list_index']==4
            s=produced['storage']
            assert produced['dtype']=='DATA_TYPE_FP16' and produced['c']==32 and produced['n']==1
            ends.append(s['src_offset_bytes']+s['surface_stride_bytes']+
                        (produced['h']-1)*s['line_stride_bytes']+produced['w']*32)
        assert max(ends)==3136
    sys.path.insert(0,str(ROOT/'runtime'))
    from loadable import Model,LoadableError
    model=Model(data)
    assert model.memory_pools==properties
    pool_start=model.layout[1]['offset']
    expected=bytearray(8192)
    expected[:2048]=blobs['tb-1'];expected[4096:4224]=blobs['tb-2']
    assert model.image[pool_start:pool_start+8192]==expected
    assert model.ranges[2]==(pool_start,8192)
    for value in (0x35,0x79):
        payload=bytes([value])*3136
        image,ranges=model.prepare({model.inputs[0].name:payload})
        assert ranges==model.ranges and image[pool_start:pool_start+8192]==expected
        start=model.inputs[0].offset
        assert image[start:start+3136]==payload
        assert image[:start]==model.image[:start] and image[start+3136:]==model.image[start+3136:]
    rejected=[]
    def reject(name,offset,replacement):
        candidate=bytearray(data);candidate[offset:offset+len(replacement)]=replacement
        try:Model(candidate)
        except LoadableError as exc:rejected.append(dict(case=name,reason=str(exc)))
        else:raise AssertionError('accepted '+name)
    content_tables={view.string(t,0):t for t in view.tables(root,5)}
    pool_property=view.vector(content_tables['property_global_dram_pool_size'],5,1).start
    for name,value in [('pool_too_small',4223),('pool_too_large',4225),('pool_over_limit',1<<63)]:
        reject(name,pool_property,struct.pack('<Q',value))
    for name,value in (('property_local_dram_pool_size',4097),('property_local_cvsram_pool_size',1)):
        reject(name,view.vector(content_tables[name],5,1).start,struct.pack('<Q',value))
    memory_table=view.tables(root,2)[1]
    reject('short_constant_memory',view.field(memory_table,3),struct.pack('<Q',4223))
    reject('constant_initialization_disabled',view.field(memory_table,2),struct.pack('<H',1))
    offsets=view.vector(memory_table,6,8)
    reject('bias_offset_overflow',offsets.start+8,struct.pack('<Q',0xffffffffffffffff))
    addresses=view.tables(root,3)
    reject('task_pool_address_short',view.field(addresses[1],3),struct.pack('<Q',4223))
    task_addresses=view.vector(view.tables(root,1)[0],3,2)
    reject('task_missing_pool',task_addresses.start+4,struct.pack('<H',0))
    # No artifact hash allowlist: modified constant bytes are still materialized.
    candidate=bytearray(data);constant_at=view.vector(content_tables['tb-1'],5,1).start
    candidate[constant_at]^=1
    changed=Model(candidate)
    assert changed.image[pool_start]==model.image[pool_start]^1
    assert changed.image[:pool_start]==model.image[:pool_start]
    assert changed.image[pool_start+1:]==model.image[pool_start+1:]
    if combined:
        scratch_start=model.layout[2]['offset']
        assert model.ranges[3]==(scratch_start,4096)
        assert model.image[scratch_start:scratch_start+4096]==bytes(4096)
        # Both uninitialized task ranges must cease to qualify; leaving the
        # compiler workspace untouched would still satisfy a capacity check.
        local_property=view.vector(content_tables['property_local_dram_pool_size'],5,1).start
        candidate=bytearray(data);struct.pack_into('<Q',candidate,local_property,4096)
        assert Model(candidate).image==model.image
        for name in ('short_scratch_ranges','initialized_scratch_ranges'):
            candidate=bytearray(data)
            for index in (2,10):
                if name=='short_scratch_ranges':
                    struct.pack_into('<Q',candidate,view.field(addresses[index],3),3135)
                else:
                    struct.pack_into('<H',candidate,view.field(view.tables(root,2)[index],2),3)
            try:Model(candidate)
            except LoadableError as exc:
                assert str(exc)=='local DRAM pool is not covered by an uninitialized task range'
                rejected.append(dict(case=name,reason=str(exc)))
            else:raise AssertionError('accepted '+name)
    print(json.dumps({'passed':True,'hardware_access':False,'loadable_sha256':digest,
                      'bytes':len(data),'backend_instructions':len(blobs['task-0-inst']),
                      'constant_bytes':[len(blobs[n]) for n in constants],
                      'global_dram_property':4224,'constant_allocation_bytes':8192,
                      'local_dram_property':properties['property_local_dram_pool_size'],
                      'native_loader_supported':True,'native_image_bytes':len(model.image),
                      'native_image_sha256':hashlib.sha256(model.image).hexdigest(),
                      'rejected_cases':rejected,
                      'limitations':['Exact original-compiler fixture, not arbitrary ONNX compilation',
                                      'Compiler describes FP19 bias storage; physical interpretation and numerical results unverified',
                                      'Local DRAM support checks range capacity, not arbitrary indirect instruction references',
                                      'No physical DLA submission or inference']},indent=2))


if __name__=='__main__':main()
