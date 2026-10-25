#!/usr/bin/env python3
"""Run the original Windows MTL factory and backend-3 constructor offline.

Allocator, memcpy and legacy backend are explicit shims. No DLL loading or
Windows APIs execute. This is not model compilation or hardware execution.
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
    pe=pefile.PE(str(path));base=pe.OPTIONAL_HEADER.ImageBase
    image=pe.get_memory_mapped_image();cases=[]
    for backend in range(5):
        u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        u.mem_map(base,(len(image)+4095)&~4095);u.mem_write(base,image)
        heap,stack,stop=0x40000000,0x41000000,0x42000000
        for page in (heap,stack,stop):u.mem_map(page,4096)
        calls=[];next_free=heap
        def hook(uc,address,size,user):
            nonlocal next_free
            a=address-base
            if a in (0xf03e10,0xf028b0):
                x0,x1=uc.reg_read(r.UC_ARM64_REG_X0),uc.reg_read(r.UC_ARM64_REG_X1)
                n=x0*x1 if a==0xf03e10 else x0
                assert 0<n<1024
                result=next_free;next_free+=(n+15)&~15
                assert next_free<heap+4096
                uc.mem_write(result,b'\0'*n if a==0xf03e10 else b'\xa5'*n)
                calls.append(dict(shim='calloc' if a==0xf03e10 else 'operator new',bytes=n))
            elif a==0xf03c10:
                calls.append(dict(shim='free'));result=0
            elif a==0x1200:
                dst,src,n=(uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2))
                assert n<1024;uc.mem_write(dst,bytes(uc.mem_read(src,n)));result=dst
                calls.append(dict(shim='memcpy',bytes=n))
            elif a==0x4cb148:
                calls.append(dict(shim='legacy mtl_init synthetic success'));result=heap+2048
            else:return
            uc.reg_write(r.UC_ARM64_REG_X0,result);uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))
        u.hook_add(UC_HOOK_CODE,hook)
        for reg,value in ((r.UC_ARM64_REG_X0,1),(r.UC_ARM64_REG_X1,7),(r.UC_ARM64_REG_X2,0x30000),
                          (r.UC_ARM64_REG_X3,backend),(r.UC_ARM64_REG_SP,stack+4096),(r.UC_ARM64_REG_LR,stop)):
            u.reg_write(reg,value)
        u.emu_start(base+0x4ca948,stop,count=2000)
        assert u.reg_read(r.UC_ARM64_REG_PC)==stop
        result=u.reg_read(r.UC_ARM64_REG_X0);assert result==(heap if backend in (1,2,3) else 0)
        item=dict(backend=backend,returns_context=bool(result),calls=calls)
        if backend==3:
            chip,observed,n,mask,obj=struct.unpack('<4IQ',u.mem_read(result,24))
            assert (chip,observed,n,mask)==(0x30000,3,1,7)
            vtable=struct.unpack('<Q',u.mem_read(obj,8))[0]
            assert vtable==base+0x114e988
            methods=struct.unpack('<11Q',u.mem_read(vtable,88))
            assert methods[5]==base+0x624eb0
            # Exercise original small methods against explicit synthetic descriptors.
            # These buffers are not compiler output or a firmware loadable.
            def call(address,*arguments):
                for reg,value in zip((r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2),arguments):
                    u.reg_write(reg,value)
                u.reg_write(r.UC_ARM64_REG_SP,stack+4096)
                u.reg_write(r.UC_ARM64_REG_LR,stop)
                u.emu_start(base+address,stop,count=500)
                assert u.reg_read(r.UC_ARM64_REG_PC)==stop
                return u.reg_read(r.UC_ARM64_REG_W0)
            assert call(0x624ea0,obj,heap+3000,heap+3100)==0
            assert struct.unpack('<2Q',u.mem_read(obj+8,16))==(heap+3000,heap+3100)
            vector,source,kindptr,destination=heap+512,heap+768,heap+900,heap+1024
            payload=b'backend-buffer-17'
            assert len(payload)==17
            u.mem_write(source,payload)
            records=[]
            for kind,length in ((0,17),(3,0),(3,17),(7,0)):
                record=bytearray(24);struct.pack_into('<Q',record,0,source)
                record[12]=kind;struct.pack_into('<Q',record,16,length);records.append(record)
            u.mem_write(vector,b''.join(records))
            u.mem_write(obj+0x58,struct.pack('<2Q',vector,vector+24*len(records)))
            getters=[]
            for kind,expected_size,copy_status in ((0,17,0),(3,17,0x7fffffff),(7,0,0x7fffffff),(8,8,2001),(9,0,2001),(2,0,0)):
                u.mem_write(kindptr,bytes([kind]));u.mem_write(destination,b'\xa5'*64)
                assert call(0x624420,obj,kindptr,destination)==0
                assert struct.unpack('<Q',u.mem_read(destination,8))[0]==expected_size
                assert bytes(u.mem_read(destination+8,56))==b'\xa5'*56
                u.mem_write(destination,b'\xa5'*64)
                status=call(0x6243a0,obj,kindptr,destination);assert status==copy_status
                # Copy selects the first matching record, unlike size lookup,
                # which skips zero-length matches. Kind 3 therefore errors.
                if kind==0:assert bytes(u.mem_read(destination,17))==payload
                else:assert bytes(u.mem_read(destination,64))==b'\xa5'*64
                if kind==0:assert bytes(u.mem_read(destination+17,47))==b'\xa5'*47
                getters.append(dict(kind=kind,size=expected_size,copy_status=status))
            item['synthetic_descriptor_cases']=getters
            item['configuration_pointer_store_verified']=True
            item.update(original_backend_constructor_executed=True,object_bytes=232,
                        vtable_rva=hex(vtable-base),method_rvas=[hex(x-base) for x in methods])
        cases.append(item)
    print(json.dumps(dict(passed=True,hardware_access=False,sha256=digest,factory_rva='0x4ca948',cases=cases,
        limitations=['Legacy initialization is stubbed, backend-3 factory and constructor execute original code',
                     'Only constructor, pointer setter and descriptor size/copy methods execute; no graph compilation or OS transport',
                     'Constructing a compiler object proves no physical NPU or firmware compatibility']),indent=2))


if __name__=='__main__':main()
