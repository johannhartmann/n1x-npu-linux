# Device-free frontend continuation using the actual Windows pretranslation
# result for the exact same five input buffers. Stops before translation.
source scripts/capture_native_mtl.gdb
python
native_sha='28668e74ced3c04ca34f30d41b7ea1d39cabe791418fbad31fcfb009d5558ad9'
windows_sha='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
conv=os.environ.get('N1X_COMPILER_MODEL')=='conv1x1'
post=pathlib.Path('results/windows-conv1x1-pretranslation' if conv else 'results/windows-pretranslation-output')
m=json.loads((post/'manifest.json').read_text())
assert m['passed'] and m['return_value']==0 and m['descriptor_unchanged']
assert m['windows_sha256']==windows_sha and m['hardware_access'] is False
assert {e['kind'] for e in m['outputs']}==set(range(5)) and len(m['outputs'])==5
# Check every input/output before modifying any inferior storage.
writes=[]
for e in m['outputs']:
    ptr=maps['pointer'][e['kind']];data=bytearray(read(ptr,e['bytes']))
    relocations={offset:bytes(data[offset:offset+8]) for offset in m.get('relocation_offsets',{}).get(str(e['kind']),[])}
    for offset in relocations:data[offset:offset+8]=bytes(8)
    assert hashlib.sha256(data).hexdigest()==e['input_sha256']
    result=bytearray((post/('kind-%d.bin'%e['kind'])).read_bytes())
    assert len(result)==len(data) and hashlib.sha256(result).hexdigest()==e['sha256']
    for offset,pointer in relocations.items():
        assert result[offset:offset+8]==bytes(8)
        result[offset:offset+8]=pointer
    writes.append((ptr,bytes(result)))
if conv:
    assert json.loads((out/'constants.json').read_text())==m['constants']
# GDB stops after the first stack-allocation instruction; `return` unwinds it.
assert int(gdb.parse_and_eval('$pc'))==int(gdb.parse_and_eval('(void*)nvdla_mtl_pre_translate'))+4
for ptr,result in writes:inf.write_memory(ptr,result)
gdb.execute('return (unsigned int)0')
end
break nvdla_mtl_translate
continue
python
assert int(gdb.parse_and_eval('$pc'))==int(gdb.parse_and_eval('(void*)nvdla_mtl_translate'))+4
common=int(gdb.parse_and_eval('$x1'));raw=read(common,96)
newdir=out/'translation-input';newdir.mkdir(exist_ok=True)
maps={name:tree(q(obj+offset),name=='index') for name,offset in [('pointer',0x60),('size',0xc0),('other',0x90),('index',0xf0)]}
assert maps['index']==({5:4,6:5,8:7,10:6,11:8} if conv else {5:3,6:4,8:6,10:5,11:7}),maps['index']
entries=[]
# MTL::translate supplies a new network descriptor, not map kind 0.
network=read(q(common+0x28),44)
assert hashlib.sha256(network).hexdigest()==('51bc13fa4d5b094618f2de1fff00e776fade8d79ccbf547d0ca69aeadbe88bcf' if conv else '8c52127eee7cbbe584ed4ea87c94632fa30c4623527560074f38f1f4b0a7d9d2'),network.hex()
assert struct.unpack_from('<I',raw,48)[0]==struct.unpack_from('<I',raw,80)[0]==0,raw.hex()
for offset,kind in ((8,2),(24,3),(32,4)):
    assert q(common+offset)==maps['pointer'][kind],(offset,kind,hex(q(common+offset)),hex(maps['pointer'][kind]))
assert raw[48:]==bytes(48)
(newdir/'network.bin').write_bytes(network)
for kind,ptr in maps['pointer'].items():
    size=maps['other'].get(kind,0)
    assert 0<=size<1024*1024
    if ptr and size:
        data=read(ptr,size);name='kind-%d.bin'%kind;(newdir/name).write_bytes(data)
        entries.append(dict(kind=kind,bytes=size,file=name,sha256=hashlib.sha256(data).hexdigest()))
(newdir/'common.bin').write_bytes(raw)
(newdir/'manifest.json').write_text(json.dumps(dict(maps=maps,entries=entries,
    native_sha256=native_sha,windows_sha256=windows_sha,
    pretranslation_manifest_sha256=hashlib.sha256((post/'manifest.json').read_bytes()).hexdigest(),
    common_sha256=hashlib.sha256(raw).hexdigest(),network_hex=network.hex(),
    network_sha256=hashlib.sha256(network).hexdigest(),
    stage='before native translation; actual Windows pretranslation outputs replayed',
    hardware_access=False,complete_loadable=False),indent=2)+'\n')
print('TRANSLATION INPUT',network.hex(),json.dumps(maps))
if conv:
    for offset,constant in zip((0x2e0,0x89e8),m['constants']):
        pointer=q(maps['pointer'][3]+offset)
        assert hashlib.sha256(read(pointer,constant['bytes'])).hexdigest()==constant['sha256']
end
