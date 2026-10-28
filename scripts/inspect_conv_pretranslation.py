#!/usr/bin/env python3
"""Run original Windows pretranslation/translation on captured convolution inputs."""
import hashlib
import json
import struct
import argparse
from functools import partial

from inspect_dla_compiler import ROOT, NATIVE_HASH
from inspect_windows_mtl_collector import main as collector


def probe(u, pe, base, invoke, allocate, *, translate=False):
    captured=ROOT/'results/native-compile-t254-conv1x1'
    manifest=json.loads((captured/'mtl-capture.json').read_text())
    expected={0:(44,'d65cf14ab07da860b3442706eb7968455cb2ca4740d8013adf7f89957a4fe96b'),
              1:(8752,'dfe06982a52556a2f02f998c8a652d2ba334899d81936ad5599fce8bbb440651'),
              2:(472,'1bfa42a3534d084fa0e5fea002683cdca70d404499093d4caf83ef2a8d1b2a0f'),
              3:(69632,'74f321f8af67f2c7a6c7ea61f3d26d59a3e2521461ae54818f6566006ff75263'),
              4:(1368,'66690b98205b2d305a0e94b4932f7dbc4894be86e6046489c5e9435eb56ba7e2')}
    buffers,pointers={},{}
    assert len(manifest['entries'])==5
    for entry in manifest['entries']:
        kind=entry['kind'];data=bytearray((captured/entry['file']).read_bytes())
        assert entry['bytes']==len(data) and entry['sha256']==hashlib.sha256(data).hexdigest()
        if kind==3:
            for offset in (0x258,0x8960):struct.pack_into('<Q',data,offset,0)
        assert (len(data),hashlib.sha256(data).hexdigest())==expected[kind]
        buffers[kind]=bytes(data);pointers[kind]=allocate(len(data))
        u.mem_write(pointers[kind],bytes(data))
    constants=json.loads((captured/'constants.json').read_text())
    assert [(c['offset'],c['bytes']) for c in constants]==[(0x258,2048),(0x8960,128)]
    constant_hashes=['599108e1d650c9e0771f84f8c9fd37c3a5637d4bcb5ad2ff3d26537c1f3b430d',
                     'c0a710f63eb066dd9b753413694963b7fc57a4cbd1a19cbe3537b19f0ca90341']
    for entry,digest in zip(constants,constant_hashes):
        data=(captured/entry['file']).read_bytes()
        assert len(data)==entry['bytes'] and hashlib.sha256(data).hexdigest()==entry['sha256']==digest
        address=allocate(len(data));u.mem_write(address,data)
        u.mem_write(pointers[3]+entry['offset'],struct.pack('<Q',address))
    common,mtl=allocate(64),allocate(512)
    descriptor=bytearray(64)
    struct.pack_into('<QI',descriptor,0,pointers[0],2)
    struct.pack_into('<3Q',descriptor,16,pointers[2],pointers[3],pointers[1])
    struct.pack_into('<Q',descriptor,48,pointers[4])
    u.mem_write(common,bytes(descriptor))
    assert invoke(0x617a70,mtl)==mtl
    status=invoke(0x624630,mtl,common,instruction_limit=10000000)
    assert status==0,hex(status)
    assert bytes(u.mem_read(common,64))==bytes(descriptor)
    outputs=[];pre_payloads={}
    outputdir=ROOT/'results/windows-conv1x1-pretranslation'
    outputdir.mkdir(exist_ok=True)
    for kind,original in buffers.items():
        data=bytearray(u.mem_read(pointers[kind],len(original)))
        if kind==3:
            for offset in (0x258,0x8960):struct.pack_into('<Q',data,offset,0)
        pre_payloads[kind]=bytes(data)
        (outputdir/f'kind-{kind}.bin').write_bytes(data)
        outputs.append({'kind':kind,'bytes':len(data),'input_sha256':expected[kind][1],
                        'sha256':hashlib.sha256(data).hexdigest(),
                        'changed_bytes':sum(a!=b for a,b in zip(original,data))})
    result={'passed':True,'hardware_access':False,'native_sha256':NATIVE_HASH,
            'windows_sha256':hashlib.sha256((ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll').read_bytes()).hexdigest(),
            'return_value':status,'descriptor_unchanged':True,'operation_count':2,'outputs':outputs,
            'constants':constants,'relocation_offsets':{'3':[0x258,0x8960]},
            'limitations':['Original Windows pretranslation only, modeled single-thread Windows/CRT',
                           'Harness runs its existing ReLU collector/dispatch checks before shared original CRT initialization',
                           'No final backend translation, packaged loadable or numerical execution']}
    (outputdir/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    if translate:
        cap=captured/'translation-input'
        capture=json.loads((cap/'manifest.json').read_text())
        assert capture['native_sha256']==NATIVE_HASH and capture['windows_sha256']==result['windows_sha256']
        assert capture['pretranslation_manifest_sha256']==hashlib.sha256((outputdir/'manifest.json').read_bytes()).hexdigest()
        input_hashes={}
        for entry in capture['entries']:
            kind=entry['kind'];data=bytearray((cap/entry['file']).read_bytes())
            assert len(data)==entry['bytes'] and hashlib.sha256(data).hexdigest()==entry['sha256']
            if kind==0:
                input_hashes[kind]=hashlib.sha256(data).hexdigest()
                continue
            expected_data=bytearray(pre_payloads[kind])
            if kind==3:
                for offset in (0x258,0x8960,0x2e0,0x89e8):struct.pack_into('<Q',data,offset,0)
                for offset,value in ((0xe8,1),(0x1e4,5),(0x2d8,2),(0x88ec,5),(0x89d8,4096),(0x89e0,2),(0x8bd0,3)):
                    struct.pack_into('<I',expected_data,offset,value)
            assert data==expected_data,kind
            input_hashes[kind]=hashlib.sha256(data).hexdigest()
            if kind==3:
                for offset,constant in zip((0x258,0x8960,0x2e0,0x89e8),constants*2):
                    payload=(captured/constant['file']).read_bytes()
                    address=allocate(len(payload));u.mem_write(address,payload)
                    struct.pack_into('<Q',data,offset,address)
            u.mem_write(pointers[kind],bytes(data))
        network=(cap/'network.bin').read_bytes()
        assert len(network)==44 and hashlib.sha256(network).hexdigest()=='51bc13fa4d5b094618f2de1fff00e776fade8d79ccbf547d0ca69aeadbe88bcf'
        address=allocate(44);u.mem_write(address,network)
        u.mem_write(common,struct.pack('<Q',address)+bytes(56))
        retained={kind:bytes(u.mem_read(pointers[kind],len(data))) for kind,data in buffers.items()}
        assert invoke(0x624eb0,mtl,common,instruction_limit=100000000)==0
        assert all(bytes(u.mem_read(pointers[k],len(v)))==v for k,v in retained.items())
        begin,end,capacity=struct.unpack('<3Q',u.mem_read(mtl+0x58,24))
        assert begin<=end<=capacity and (end-begin)%24==0 and end-begin<=24*16
        final=[];payloads={}
        pinned={0:(44544,'3e343cadea1139825e1a078eadd2c1869d9f59ffd718714eb98f016b30d1525e'),
                1:(320,'43cc4af3e7149b7e3281eac118f69132e255db75210896f49cf140f2d5f06db3'),
                3:(256,'83e5085f97da6691fc8729d6cb815a6c6829925ed5fd28998c314fcb7d9e6803'),
                4:(2738944,'1213200f5d0ad268d324b62729dbcd520ff32f4cb94b78bf5afcf536cc1b11a4'),
                5:(28736,'46986c9b500f0d60ee8b2342ea64064d5d56493cb15f978b4a8cec36cb33b320'),
                6:(0,'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
                7:(19034,'daa29756f00ee352a2a05aec509fee0ae7cfd7de83807e9e6610a1eb25c0f1c8')}
        outdir=ROOT/'results/windows-conv1x1-backend';outdir.mkdir(exist_ok=True)
        for offset in range(begin,end,24):
            raw=bytes(u.mem_read(offset,24))
            pointer=struct.unpack_from('<Q',raw)[0];kind=raw[12]
            size=struct.unpack_from('<Q',raw,16)[0]
            assert size<=16*1024*1024
            payload=bytes(u.mem_read(pointer,size)) if size else b''
            digest=hashlib.sha256(payload).hexdigest()
            assert kind not in payloads and (size,digest)==pinned[kind]
            payloads[kind]=payload
            key,got_size,destination=allocate(1),allocate(16),allocate(size+16)
            u.mem_write(key,bytes([kind]));u.mem_write(got_size,b'\xa5'*16)
            assert invoke(0x624420,mtl,key,got_size)==0
            assert bytes(u.mem_read(got_size,16))==struct.pack('<Q',size)+b'\xa5'*8
            assert invoke(0x6243a0,mtl,key,destination)==(0 if size else 0x7fffffff)
            assert bytes(u.mem_read(destination,size+16))==payload+b'\xa5'*16
            final.append({'kind':kind,'bytes':size,'file':f'kind-{kind}.bin','sha256':digest})
        assert set(payloads)==set(pinned)
        for kind,payload in payloads.items():(outdir/f'kind-{kind}.bin').write_bytes(payload)
        result['translation']={'return_value':0,'outputs':final,'complete_loadable':False}
        result['handoff']={'input_sha256':input_hashes,'network_sha256':hashlib.sha256(network).hexdigest(),
                           'pointer_offsets':{'3':[0x258,0x8960,0x2e0,0x89e8]},
                           'inputs_unchanged_after_translation':True}
        result['limitations'][0]='Original Windows pretranslation and translation, modeled single-thread Windows/CRT'
        result['limitations'][-1]='No packaged loadable or numerical execution'
        (outdir/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--translate',action='store_true')
    args=parser.parse_args()
    print(json.dumps(collector([],initialized_probe=partial(probe,translate=args.translate)),indent=2))
