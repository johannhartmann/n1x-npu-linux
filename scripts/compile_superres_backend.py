#!/usr/bin/env python3
"""Run original Windows backend passes for the captured super-resolution graph.

Offline only. Uses the existing initialized Windows/CRT emulator; constants
are relocated explicitly and every handoff is recorded by content hash.
"""
import argparse
import hashlib
import json
import struct
from functools import partial
import sys
from inspect_dla_compiler import ROOT,NATIVE_HASH
from inspect_windows_mtl_collector import main as collector
from unicorn import UC_HOOK_MEM_WRITE,UC_HOOK_CODE
from unicorn import arm64_const as registers


def digest(data):return hashlib.sha256(data).hexdigest()


def probe(u,pe,base,invoke,allocate,*,translate=False):
    captured=ROOT/'results/native-compile-t254-superres'
    manifest=json.loads((captured/'mtl-capture.json').read_text())
    constants=json.loads((captured/'constants.json').read_text())
    spec=json.loads((captured/'constants-spec.json').read_text())
    assert spec['operation_count']==11 and len(constants)==8
    constant_guards=[]
    translated_constants={}
    def constant_buffer(payload,entry):
        # Original bias conversion writes complete 128-byte FP19 channel
        # blocks. Logical channel count (e.g. nine) is not backing capacity.
        # T254 weight blocks have 16 input/32 output FP16 channels; the
        # compiler packs them in place. Capture length is logical data only.
        capacity=(max(len(payload),entry['backing_bytes'])+4095)&~4095
        pointer=allocate(capacity+128)
        u.mem_write(pointer,bytes(capacity));u.mem_write(pointer,payload)
        constant_guards.append((pointer+capacity,128))
        return pointer
    raw,pointers={},{}
    for entry in manifest['entries']:
        data=bytearray((captured/entry['file']).read_bytes())
        assert len(data)==entry['bytes'] and digest(data)==entry['sha256']
        if entry['kind']==3:
            for constant in constants:struct.pack_into('<Q',data,constant['offset'],0)
        raw[entry['kind']]=bytes(data)
        pointers[entry['kind']]=allocate(len(data));u.mem_write(pointers[entry['kind']],bytes(data))
    assert set(raw)==set(range(5))
    assert len(raw[1])==11*4376 and len(raw[2])==11*236 and len(raw[3])==11*34816
    for entry in constants:
        payload=(captured/entry['file']).read_bytes()
        assert len(payload)==entry['bytes'] and digest(payload)==entry['sha256']
        address=constant_buffer(payload,entry)
        u.mem_write(pointers[3]+entry['offset'],struct.pack('<Q',address))
    common,mtl=allocate(64),allocate(512)
    descriptor=bytearray(64)
    struct.pack_into('<QI',descriptor,0,pointers[0],11)
    struct.pack_into('<3Q',descriptor,16,pointers[2],pointers[3],pointers[1])
    struct.pack_into('<Q',descriptor,48,pointers[4])
    u.mem_write(common,bytes(descriptor))
    assert invoke(0x617a70,mtl)==mtl
    status=invoke(0x624630,mtl,common,instruction_limit=200000000)
    assert status==0,hex(status)
    assert bytes(u.mem_read(common,64))==bytes(descriptor)
    output=ROOT/'results/windows-superres-pretranslation';output.mkdir(exist_ok=True)
    entries=[]
    for kind,data in raw.items():
        result=bytearray(u.mem_read(pointers[kind],len(data)))
        if kind==3:
            for entry in constants:struct.pack_into('<Q',result,entry['offset'],0)
        (output/f'kind-{kind}.bin').write_bytes(result)
        entries.append(dict(kind=kind,bytes=len(result),input_sha256=digest(data),sha256=digest(result)))
    windows_sha=digest((ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll').read_bytes())
    record=dict(passed=True,return_value=0,descriptor_unchanged=True,hardware_access=False,
                native_sha256=NATIVE_HASH,windows_sha256=windows_sha,operation_count=11,
                outputs=entries,constants=constants,relocation_offsets={'3':[e['offset'] for e in constants]})
    (output/'manifest.json').write_text(json.dumps(record,indent=2)+'\n')
    if not translate:return record
    cap=captured/'translation-input';capture=json.loads((cap/'manifest.json').read_text())
    assert capture['pretranslation_manifest_sha256']==digest((output/'manifest.json').read_bytes())
    offsets=[offset for entry in constants for offset in (entry['offset'],entry['offset']+136)]
    hashes={}
    for entry in capture['entries']:
        kind=entry['kind'];data=bytearray((cap/entry['file']).read_bytes())
        assert len(data)==entry['bytes'] and digest(data)==entry['sha256']
        # preTranslate updates its retained network descriptor in place.
        # The native capture's kind-0 map is the earlier input, not the fresh
        # network descriptor passed separately to translate. Preserve the
        # Windows preTranslate-owned state, as in the qualified Conv bridge.
        if kind==0:
            hashes[kind]=digest(data)
            continue
        normalized=bytearray(data)
        if kind==3:
            for offset in offsets:struct.pack_into('<Q',normalized,offset,0)
            for constant in constants:
                payload=(captured/constant['file']).read_bytes()
                for offset in (constant['offset'],constant['offset']+136):
                    address=constant_buffer(payload,constant)
                    struct.pack_into('<Q',data,offset,address)
                    if offset==constant['offset']+136:translated_constants[constant['offset']]=address
        hashes[kind]=digest(normalized)
        u.mem_write(pointers[kind],bytes(data))
    network=(cap/'network.bin').read_bytes();assert len(network)==44
    address=allocate(44);u.mem_write(address,network)
    translation_common=allocate(64)
    u.mem_write(translation_common,struct.pack('<Q',address)+bytes(56))
    watched_writes=[]
    original_write=u.mem_write
    network_address=address
    def watched_write(target,data):
        if target<translation_common+64 and target+len(data)>network_address:
            watched_writes.append(dict(source='modeled',pc=hex(u.reg_read(registers.UC_ARM64_REG_PC)-base),
                                       address=hex(target),bytes=len(data),prefix=bytes(data[:64]).hex()))
        return original_write(target,data)
    u.mem_write=watched_write
    def watched_native(machine,access,target,size,value,_):
        watched_writes.append(dict(source='original',pc=hex(machine.reg_read(registers.UC_ARM64_REG_PC)-base),
                                   address=hex(target),bytes=size,value=hex(value)))
    watch_hook=u.hook_add(UC_HOOK_MEM_WRITE,watched_native,begin=network_address,end=translation_common+63)
    retained={kind:bytes(u.mem_read(ptr,len(raw[kind]))) for kind,ptr in pointers.items()}
    enum_writes={}
    def enum_write(machine,access,address,size,value,_):
        if size==1 and value<8:
            enum_writes[address]=dict(value=value,pc=hex(machine.reg_read(registers.UC_ARM64_REG_PC)-base))
    trace_hook=u.hook_add(UC_HOOK_MEM_WRITE,enum_write,begin=0x40000000,end=0x48000000-1)
    blob_ids=[]
    def blob_key(machine,pc,size,_):
        source=machine.reg_read(registers.UC_ARM64_REG_X19)
        arg=machine.reg_read(registers.UC_ARM64_REG_X25)
        pointer=struct.unpack('<Q',machine.mem_read(arg,8))[0]
        blob_ids.append(dict(id=struct.unpack('<I',machine.mem_read(source,4))[0],
                            network=list(struct.unpack('<11I',machine.mem_read(pointer,44)))))
    key_hook=u.hook_add(UC_HOOK_CODE,blob_key,begin=base+0x625a38,end=base+0x625a38)
    try:
        assert invoke(0x624eb0,mtl,translation_common,instruction_limit=500000000)==0
    except Exception:
        (ROOT/'results/superres-translation-failure.json').write_text(json.dumps(dict(
            pc=hex(u.reg_read(registers.UC_ARM64_REG_PC)-base),blob_ids=blob_ids,writes=watched_writes,
            registers=[hex(u.reg_read(getattr(registers,f'UC_ARM64_REG_X{i}'))) for i in range(29)]),indent=2)+'\n')
        raise
    u.hook_del(key_hook)
    assert all(bytes(u.mem_read(pointer,size))==b'\xa5'*size for pointer,size in constant_guards),'constant padding overrun'
    u.hook_del(trace_hook)
    assert all(bytes(u.mem_read(pointers[kind],len(data)))==data for kind,data in retained.items())
    begin,end,capacity=struct.unpack('<3Q',u.mem_read(mtl+0x58,24))
    assert begin<=end<=capacity and (end-begin)%24==0 and end-begin<=24*16
    final=ROOT/'results/windows-superres-backend';final.mkdir(exist_ok=True)
    blobs=[]
    for offset in range(begin,end,24):
        entry=bytes(u.mem_read(offset,24));pointer=struct.unpack_from('<Q',entry)[0]
        kind=entry[12];size=struct.unpack_from('<Q',entry,16)[0]
        assert size<=32<<20
        data=bytes(u.mem_read(pointer,size)) if size else b''
        (final/f'kind-{kind}.bin').write_bytes(data)
        blobs.append(dict(kind=kind,bytes=size,file=f'kind-{kind}.bin',sha256=digest(data),record_hex=entry.hex(),
                          kind_store=enum_writes.get(offset+12)))
    (final/'observation.json').write_text(json.dumps(dict(outputs=blobs,diagnostics=u.n1x_compiler_diagnostics,blob_ids=blob_ids),indent=2)+'\n')
    assert {b['kind'] for b in blobs}=={0,1,3,4,5,6,7}
    record.update(translation=dict(return_value=0,outputs=blobs),
                  handoff=dict(input_sha256=hashes,network_sha256=digest(network),
                               pointer_offsets={'3':offsets},inputs_unchanged_after_translation=True))
    # The native frontend owns destination constants separately from the
    # logical source arrays. Export their original Windows packed contents.
    # A bootstrap serialization supplies the frontend's exact content extents;
    # it is not numerically qualified until this constant replay is applied.
    serialized=captured/'superres.nvda'
    if serialized.exists():
        sys.path.insert(0,str(ROOT/'runtime'))
        from loadable import Reader
        reader=Reader(serialized.read_bytes());root=reader.table(reader.number('<I',0),10)
        contents={b.name(0):len(b.blob(5)) for b in root.tables(5,7)}
        memories=[m for m in root.tables(2,9) if m.value(2,'<H')==3 and m.names(5)
                  and all(name.startswith('tb-') for name in m.names(5))]
        assert len(memories)==1
        names=[name for _,name in sorted(zip(memories[0].numbers(6,'<Q'),memories[0].names(5)))];assert len(names)==len(constants)
        packed=[]
        for name,entry in zip(names,constants):
            size=contents[name];assert size<=((entry['backing_bytes']+4095)&~4095)
            payload=bytes(u.mem_read(translated_constants[entry['offset']],size))
            filename=f'packed-{name}.bin';(final/filename).write_bytes(payload)
            packed.append(dict(name=name,offset=entry['offset']+136,bytes=size,file=filename,sha256=digest(payload)))
        record['constant_outputs']=packed
    (final/'manifest.json').write_text(json.dumps(record,indent=2)+'\n')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--translate',action='store_true')
    args=parser.parse_args()
    print(json.dumps(collector([],initialized_probe=partial(probe,translate=args.translate)),indent=2))
