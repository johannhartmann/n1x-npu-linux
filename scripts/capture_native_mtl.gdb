set startup-with-shell off
set pagination off
set debuginfod enabled off
set breakpoint pending on
break _ZN5nvdla4priv3MTL12preTranslateEPvb
commands
silent
set $n1x_mtl=$x0
continue
end
break nvdla_mtl_pre_translate
run
python
import gdb,struct,json,hashlib,pathlib,os
inf=gdb.selected_inferior()
def read(a,n): return bytes(inf.read_memory(a,n))
def q(a): return struct.unpack('<Q',read(a,8))[0]
obj=int(gdb.parse_and_eval('$n1x_mtl'))
common=int(gdb.parse_and_eval('$x1'))
def tree(root,index=False):
    result={};seen=set()
    def walk(n):
        if not n:return
        assert n not in seen and len(seen)<128;seen.add(n)
        walk(q(n+16))
        result[struct.unpack('<i',read(n+32,4))[0]]=struct.unpack('<i',read(n+36,4))[0] if index else q(n+40)
        walk(q(n+24))
    walk(root);return result
maps={name:tree(q(obj+offset),name=='index') for name,offset in [('pointer',0x60),('size',0xc0),('other',0x90),('index',0xf0)]}
out=pathlib.Path(os.environ['N1X_MTL_CAPTURE_DIR']);entries=[]
for kind,ptr in maps['pointer'].items():
    size=maps['other'].get(kind,0)
    assert 0<=size<1024*1024
    if ptr and size:
        data=read(ptr,size);name='mtl-kind-%d.bin'%kind;(out/name).write_bytes(data)
        entries.append(dict(kind=kind,pointer=ptr,bytes=size,file=name,sha256=hashlib.sha256(data).hexdigest()))
(out/'mtl-common.bin').write_bytes(read(common,96))
(out/'mtl-capture.json').write_text(json.dumps(dict(maps=maps,entries=entries,common_pointer=common,hardware_access=False,stage='before native MTL pre-translation',limitations=['Intermediate buffers only; no firmware loadable or inference', 'Pointers are process-local; not reusable across executions']),indent=2)+'\n')
print('CAPTURED',json.dumps(maps),entries)
if os.environ.get('N1X_COMPILER_MODEL') in ('conv1x1','conv1x1_relu'):
    constants=[]
    for offset,size in ((0x258,2048),(0x8960,128)):
        pointer=q(maps['pointer'][3]+offset)
        data=read(pointer,size);name='constant-%x.bin'%offset
        (out/name).write_bytes(data)
        constants.append(dict(offset=offset,bytes=size,file=name,sha256=hashlib.sha256(data).hexdigest()))
    (out/'constants.json').write_text(json.dumps(constants,indent=2)+'\n')
end
