#!/usr/bin/env python3
"""Check native blob consumers against extracted Windows backend buffers.

Executes original native getters and Windows copy code in one Unicorn address
space. The ABI adapter, frontend size/map operations and memcpy are explicit
fixtures. This does not run the frontend packager or produce a loadable.
"""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT, NATIVE_HASH


def main():
    directory=ROOT/'sources/tensorrt-11.4'
    native=(directory/'native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so').read_bytes()
    windows=directory/'nvdla_compiler.dll'
    windows_hash=hashlib.sha256(windows.read_bytes()).hexdigest()
    assert hashlib.sha256(native).hexdigest()==NATIVE_HASH
    assert windows_hash=='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
    phoff=struct.unpack_from('<Q',native,32)[0]
    phsize,count=struct.unpack_from('<HH',native,54)
    segments=[struct.unpack_from('<IIQQQQQQ',native,phoff+i*phsize) for i in range(count)]
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for page in (0x588000,0x589000,0x58b000,0x649000):
        segment=next(s for s in segments if s[0]==1 and s[3]<=page and page+4096<=s[3]+s[5])
        offset=segment[2]+page-segment[3]
        u.mem_map(page,4096);u.mem_write(page,native[offset:offset+4096])
    pe=pefile.PE(str(windows));base=pe.OPTIONAL_HEADER.ImageBase
    image=pe.get_memory_mapped_image()
    u.mem_map(base,(len(image)+4095)&~4095);u.mem_write(base,image)
    heap,stack,stop=0x40000000,0x41000000,0x42000000
    u.mem_map(heap,0x40000);u.mem_map(stack,0x4000);u.mem_map(stop,4096)
    # Only the stack guard GOT slot is needed by these native functions.
    u.mem_map(0xc8c000,4096)
    u.mem_write(0xc8caa8,struct.pack('<Q',heap+0x700))
    frontend,handle,backend,vtable,vector=heap,heap+0x200,heap+0x300,heap+0x400,heap+0x500
    u.mem_write(frontend+0x10,b'\x03')
    u.mem_write(frontend+0x18,struct.pack('<Q',handle))
    u.mem_write(handle,struct.pack('<4IQ',0x30000,3,1,7,backend))
    u.mem_write(backend,struct.pack('<Q',vtable))
    # The two compilers have different C++ vtables. This is an explicit adapter,
    # not evidence that their objects can be exchanged without translation.
    u.mem_write(vtable+0x50,struct.pack('<Q',base+0x6243a0))
    pinned={
        0:(0,'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
        3:(256,'d40c6354059292dc4b2c3c7faca391b59240d305ce45b35da00c902ca67f778c'),
        6:(28736,'da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3'),
    }
    cursor=heap+0x1000;payloads={}
    for index,(kind,(length,digest)) in enumerate(pinned.items()):
        data=(ROOT/f'results/windows-backend-output/kind-{kind}.bin').read_bytes()
        assert len(data)==length and hashlib.sha256(data).hexdigest()==digest
        if data:u.mem_write(cursor,data)
        descriptor=bytearray(24)
        struct.pack_into('<Q',descriptor,0,cursor);descriptor[12]=kind
        struct.pack_into('<Q',descriptor,16,length)
        u.mem_write(vector+index*24,bytes(descriptor))
        payloads[kind]=data;cursor+=(length+15)&~15
    u.mem_write(backend+0x58,struct.pack('<3Q',vector,vector+72,vector+72))
    destination=heap+0x20000
    copies=[];registrations=[];size_queries=[]

    def hook(uc,address,size,user):
        x0,x1,x2=[uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2)]
        if address==0x58b970:
            assert (x0,x1)==(frontend,0)
            size_queries.append(x1);uc.mem_write(x2,struct.pack('<Q',len(payloads[3])));result=0
        elif address==0x5885a0:
            assert x0==frontend and x2==destination
            registrations.append(x1);result=0
        elif address==base+0x6243a0:
            assert x0==backend and x2==destination
            copies.append(uc.mem_read(x1,1)[0]);return  # Execute original getter.
        elif address==base+0x1200:
            assert x0==destination and x2 in (256,28736)
            uc.mem_write(x0,bytes(uc.mem_read(x1,x2)));result=x0
        else:raise AssertionError(hex(address))
        uc.reg_write(r.UC_ARM64_REG_X0,result)
        uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))

    for address in (0x58b970,0x5885a0,base+0x6243a0,base+0x1200):
        u.hook_add(UC_HOOK_CODE,hook,begin=address,end=address)
    cases=[]
    for name,address,kind,memory_kind,status in (
        ('network_descriptor',0x58ba64,3,0,0),
        ('rvv',0x589b40,6,11,0),
        ('instruction',0x589aa0,0,None,8),
    ):
        copies.clear();registrations.clear();size_queries.clear()
        u.mem_write(destination,b'\xa5'*0x8000)
        for reg,value in ((r.UC_ARM64_REG_X0,frontend),(r.UC_ARM64_REG_X1,destination),
                          (r.UC_ARM64_REG_SP,stack+0x4000),(r.UC_ARM64_REG_LR,stop)):
            u.reg_write(reg,value)
        u.emu_start(address,stop,count=10000)
        assert u.reg_read(r.UC_ARM64_REG_PC)==stop
        assert u.reg_read(r.UC_ARM64_REG_X0)==status
        assert copies==[kind]
        assert registrations==([] if memory_kind is None else [memory_kind])
        assert size_queries==([0] if kind==3 else [])
        expected=payloads[kind]+b'\xa5'*(0x8000-len(payloads[kind]))
        assert bytes(u.mem_read(destination,0x8000))==expected
        cases.append(dict(consumer=name,elf_address=hex(address),backend_kind=kind,
                          frontend_memory_kind=memory_kind,status=status,bytes=len(payloads[kind]),
                          copy_and_guard_verified=True))
    print(json.dumps(dict(passed=True,hardware_access=False,native_sha256=NATIVE_HASH,
                         windows_sha256=windows_hash,cases=cases,
                         limitations=[
                             'Explicit vtable adapter; frontend memory registration and size lookup are fixtures',
                             'Only selected blob consumers execute, not complete frontend packaging',
                             'Kind 7 JSON is not classified by this probe',
                             'No loadable, firmware acceptance or physical inference is established']),indent=2))


if __name__=='__main__':main()
