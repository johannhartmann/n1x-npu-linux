#!/usr/bin/env python3
"""Replay original Windows MTL input collection using captured ReLU buffers.

Only memcpy is shimmed. Original stack-cookie and stack-probe helpers execute
with modeled stack/TEB memory. No OS call, graph translation or device executes.
"""
import hashlib,json,struct
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT


def main():
    path=ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest=='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
    captured=ROOT/'results/native-compile-t254'
    manifest=json.loads((captured/'mtl-capture.json').read_text())
    entries={e['kind']:e for e in manifest['entries']}
    expected={0:44,1:4376,2:236,3:34816,4:1368}
    buffers={}
    for kind,size in expected.items():
        entry=entries[kind];data=(captured/entry['file']).read_bytes()
        assert entry['bytes']==len(data)==size
        assert hashlib.sha256(data).hexdigest()==entry['sha256']
        buffers[kind]=data
    pe=pefile.PE(str(path));base=pe.OPTIONAL_HEADER.ImageBase
    image=pe.get_memory_mapped_image();u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    u.mem_map(base,(len(image)+4095)&~4095);u.mem_write(base,image)
    heap,stack,stop,teb=0x40000000,0x41000000,0x42000000,0x43000000
    u.mem_map(heap,0x100000);u.mem_map(stack,0x40000)
    u.mem_write(stack,b'\xa5'*0x40000)
    u.mem_map(stop,4096);u.mem_map(teb,4096)
    u.mem_write(teb+0x10,struct.pack('<Q',stack))
    pointers={};cursor=heap+0x1000
    for kind,data in buffers.items():
        pointers[kind]=cursor;u.mem_write(cursor,data);cursor+=(len(data)+4095)&~4095
    common,vector,output=heap+0x100,heap+0x200,heap+0x20000
    # Layout copied by Windows wrapper 0x4cab40; host pointers are relocated.
    descriptor=bytearray(64)
    struct.pack_into('<QI',descriptor,0,pointers[0],1)
    struct.pack_into('<3Q',descriptor,16,pointers[2],pointers[3],pointers[1])
    u.mem_write(common,bytes(descriptor))
    u.mem_write(vector,struct.pack('<3Q',output,output,output+0x9f60))
    copies=[]
    def hook(uc,address,size,user):
        if address!=base+0x1200:return
        dst,src,n=(uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2))
        assert 0<n<=0x9f60
        uc.mem_write(dst,bytes(uc.mem_read(src,n)));copies.append(n)
        uc.reg_write(r.UC_ARM64_REG_X0,dst)
        uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    for reg,value in ((r.UC_ARM64_REG_X0,0),(r.UC_ARM64_REG_X1,common),(r.UC_ARM64_REG_X2,vector),
                      (r.UC_ARM64_REG_X18,teb),(r.UC_ARM64_REG_SP,stack+0x40000),(r.UC_ARM64_REG_LR,stop)):
        u.reg_write(reg,value)
    u.emu_start(base+0x756630,stop,count=20000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==stop
    assert struct.unpack('<3Q',u.mem_read(vector,24))==(output,output+0x9f60,output+0x9f60)
    record=bytes(u.mem_read(output,0x9f60))
    assert record[:236]==buffers[2]
    assert record[236:240]==b'\xa5'*4
    assert record[0xf0:0x88f0]==buffers[3]
    assert record[0x88f0:0x9a08]==buffers[1]
    assert record[0x9a08:]==b'\xa5'*(0x9f60-0x9a08)
    assert copies==[0x8800,0x1118,0x9f60]
    print(json.dumps(dict(passed=True,hardware_access=False,windows_sha256=digest,
        collector_rva='0x756630',operation_count=1,record_bytes=len(record),
        original_stack_helpers_executed=True,memcpy_shim_sizes=copies,
        verified_regions=[dict(offset=0,bytes=236,source_kind=2),dict(offset=240,bytes=34816,source_kind=3),
                          dict(offset=35056,bytes=4376,source_kind=1)],
        untouched_regions=[dict(offset=236,bytes=4),dict(offset=39432,bytes=1368)],
        limitations=['One preallocated collector record; vector growth is not executed',
                     'Copied buffers match the captured native input, not translated firmware output',
                     'No backend configuration, instruction lowering, loadable or inference is established']),indent=2))


if __name__=='__main__':main()
