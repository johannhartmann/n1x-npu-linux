#!/usr/bin/env python3
"""Verify the serialized ReLU artifact against actual Windows backend outputs.

Uses the bounded shared-field FlatBuffers projection. This is not a complete
0.9.0 schema verifier, runtime acceptance test, or numerical inference test.
"""
import hashlib
import json
from inspect_dla_compiler import ROOT
from inspect_loadable import Buffer,inspect

LOADABLE_HASH='561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942'


def main():
    directory=ROOT/'results/native-compile-t254'
    data=(directory/'relu.nvda').read_bytes()
    assert len(data)==2800596 and hashlib.sha256(data).hexdigest()==LOADABLE_HASH
    manifest=json.loads((directory/'manifest.json').read_text())
    assert manifest['sha256']==LOADABLE_HASH and manifest['bytes']==len(data)
    assert manifest['backend']=='verified Windows result replay' and not manifest['inference_verified']
    replay=json.loads((directory/'packaging-replay.json').read_text())
    assert replay['passed'] and not replay['hardware_access']
    backend=ROOT/'results/windows-final-backend-output'
    assert hashlib.sha256((backend/'manifest.json').read_bytes()).hexdigest()==replay['backend_manifest_sha256']
    backend_manifest=json.loads((backend/'manifest.json').read_text())
    outputs={entry['kind']:entry for entry in backend_manifest['outputs']}
    projected=inspect(data);view=Buffer(data);root=view.u('<I',0)
    assert projected['version_bytes']==[0,9,0]
    assert projected['root_vector_lengths']=={'1':1,'2':9,'3':9,'4':1,'5':8,'6':2,'7':0,'8':1}
    assert projected['tasks']==[dict(id=0,interface=1,instance=-1,
        address_ids=[3,1,2,4,5,6,7,8],pre_actions=[],post_actions=[0])]
    assert projected['submits']==[dict(id=0,task_ids=[0])]
    symbols={'task-0-nw':3,'task-0-inst':0,'task-0-ctrl_data':1,
             'task-0-preempt_ctx':4,'task-0-stats':5}
    rows=[]
    for table in view.tables(root,5):
        name=view.string(table,0);extent=view.vector(table,5,1)
        payload=data[extent.start:extent.stop]
        assert view.scalar(table,1,'<Q')==len(payload)
        version_at=view.field(table,4);assert version_at is not None
        version=list(data[version_at:version_at+3])
        if name in symbols:
            kind=symbols[name];expected=(backend/f'kind-{kind}.bin').read_bytes()
            assert hashlib.sha256(expected).hexdigest()==outputs[kind]['sha256']
            assert payload==expected
            assert version==[3,0,0] and view.scalar(table,2,'<I')==1
        else:
            assert name in ('property_global_dram_pool_size','property_local_cvsram_pool_size','property_local_dram_pool_size')
            assert len(payload)==8 and version==[0,0,0]
        rows.append(dict(name=name,bytes=len(payload),version=version,sha256=hashlib.sha256(payload).hexdigest()))
    memories=projected['memory'];addresses=projected['addresses']
    assert [m['id'] for m in memories]==list(range(9))
    assert [a['id'] for a in addresses]==list(range(9))
    for memory,address in zip(memories,addresses):
        assert memory['domain']==0 and memory['alignment']==4096
        assert address['memory_id']==memory['id'] and address['offset']==0 and address['size']==memory['size']
        assert memory['content_count']==len(memory['content_offsets'])
        for name,offset in zip(memory['contents'],memory['content_offsets']):
            assert name in symbols and offset==0
            assert outputs[symbols[name]]['bytes']==memory['size']
    for index,flags in ((1,5),(2,9)):
        assert memories[index]['size']==3136 and memories[index]['flags']==flags
        assert not memories[index]['contents']
    # These values are compiler output, not guessed replacements for the
    # legacy diagnostic's absent extension fields. Their hardware behavior
    # still requires separate firmware/IRQ verification.
    network=(backend/'kind-3.bin').read_bytes()
    network_view=Buffer(network)
    extension={hex(offset):network_view.u('<I',offset) for offset in (0xcc,0xd0,0xd4,0xd8)}
    assert extension=={'0xcc':0x3054,'0xd0':4,'0xd4':0x3054,'0xd8':3}
    print(json.dumps(dict(passed=True,hardware_access=False,bytes=len(data),sha256=LOADABLE_HASH,
        serialized_version=[0,9,0],tasks=projected['tasks'],contents=rows,
        compiler_network_extension=extension,
        limitations=['Shared FlatBuffers fields only, not a complete 0.9.0 schema validator',
                     'Native serialization uses verified Windows result replay for one exact graph',
                     'Runtime acceptance, firmware compatibility and numerical NPU execution remain unverified']),indent=2))


if __name__=='__main__':main()
