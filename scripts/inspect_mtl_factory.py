#!/usr/bin/env python3
"""Execute the pinned native MTL factory with explicit allocator/backend shims.

No native library loading, device access, or model execution takes place.
"""
import hashlib
import json
import struct
from pathlib import Path
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT, NATIVE_HASH


def main():
    data=(ROOT/'sources/tensorrt-11.4/native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so').read_bytes()
    assert hashlib.sha256(data).hexdigest()==NATIVE_HASH
    phoff=struct.unpack_from('<Q',data,32)[0]
    phsize,count=struct.unpack_from('<HH',data,54)
    segments=[struct.unpack_from('<IIQQQQQQ',data,phoff+i*phsize) for i in range(count)]
    def read(address,size):
        for kind,flags,offset,va,pa,fs,ms,align in segments:
            if kind==1 and va<=address and address+size<=va+fs:
                return data[offset+address-va:offset+address-va+size]
        raise ValueError(hex(address))
    cases=[]
    for backend in range(5):
        u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        u.mem_map(0x648000,4096);u.mem_write(0x648000,read(0x648000,4096))
        for page in (0xaa000,0xab000,0x649000):u.mem_map(page,4096)
        heap,stack,stop=0x40000000,0x41000000,0x42000000
        for page in (heap,stack,stop):u.mem_map(page,4096)
        calls=[]
        def hook(uc,address,size,user):
            if address==0xaaf50:
                assert uc.reg_read(r.UC_ARM64_REG_X0)==1 and uc.reg_read(r.UC_ARM64_REG_X1)==24
                calls.append('calloc(1,24)');uc.mem_write(heap,b'\0'*24);result=heap
            elif address==0xab430:
                assert uc.reg_read(r.UC_ARM64_REG_X0)==heap
                calls.append('free');result=0
            elif address==0x649be0:
                assert uc.reg_read(r.UC_ARM64_REG_X0)==1 and uc.reg_read(r.UC_ARM64_REG_X1)==7
                calls.append('mtl_init (stubbed success)');result=heap+256
            else:return
            uc.reg_write(r.UC_ARM64_REG_X0,result)
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))
        u.hook_add(UC_HOOK_CODE,hook)
        for reg,value in ((r.UC_ARM64_REG_X0,1),(r.UC_ARM64_REG_X1,7),
                          (r.UC_ARM64_REG_X2,0x30000),(r.UC_ARM64_REG_X3,backend),
                          (r.UC_ARM64_REG_SP,stack+4096),(r.UC_ARM64_REG_LR,stop)):
            u.reg_write(reg,value)
        u.emu_start(0x648150,stop,count=200)
        assert u.reg_read(r.UC_ARM64_REG_PC)==stop
        result=u.reg_read(r.UC_ARM64_REG_X0)
        assert result==(heap if backend in (1,2) else 0)
        assert ('mtl_init (stubbed success)' in calls)==(backend in (1,2))
        assert ('free' in calls)==(backend not in (1,2))
        cases.append(dict(backend=backend,returns_context=bool(result),calls=calls))
    print(json.dumps(dict(passed=True,hardware_access=False,native_sha256=NATIVE_HASH,
        original_factory_elf_address='0x648150',cases=cases,
        limitations=['calloc/free and legacy mtl_init are modeled, not executed',
                     'Backend 3 rejection is proved for this pinned ELF only',
                     'No Windows compiler backend or physical firmware compatibility is established']),indent=2))


if __name__=='__main__':main()
