#!/usr/bin/env python3
"""Execute the recovered private-query producer prefix, without hardware."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_runtime_selector import ROOT

BASE = 0x140000000
KMD_HASH = '73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987'
OBJ, QUERY, OUTPUT, STACK, STOP = (0x41000000+i*0x20000 for i in range(5))


def run(pe, word, length=0xc5b8, null=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0x19df000,0x19dc000,0xe3d000):
        u.mem_map(BASE+page,4096)
        u.mem_write(BASE+page,pe.get_data(page,4096))
    for address in (OBJ, QUERY, OUTPUT, STACK, STOP):u.mem_map(address,0x20000)
    u.mem_write(QUERY+0x18,struct.pack('<Q',0 if null else OUTPUT))
    u.mem_write(QUERY+0x20,struct.pack('<I',length))
    u.mem_write(OUTPUT,b'\xa5'*0xc5b8)
    u.mem_write(OBJ+0x4648,struct.pack('<II',word,word^0xffffffff))
    helpers=[]
    def hook(_,address,size,user):
        if address==BASE+0xe3d050:
            dest,arg,count=[u.reg_read(getattr(r,f'UC_ARM64_REG_X{i}')) for i in range(3)]
            assert (dest,arg,count)==(OUTPUT+0x14,0,0xc5a4)
            u.mem_write(dest,bytes(count));helpers.append('zero-private-tail')
        elif address==BASE+0x19dc250:
            assert u.reg_read(r.UC_ARM64_REG_X0)==OBJ+0x19f0
            assert u.reg_read(r.UC_ARM64_REG_X1)==OUTPUT+0x20
            helpers.append('other-private-field-builder')
        else:return
        u.reg_write(r.UC_ARM64_REG_X0,0)
        u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    u.reg_write(r.UC_ARM64_REG_X0,OBJ);u.reg_write(r.UC_ARM64_REG_X1,QUERY)
    u.reg_write(r.UC_ARM64_REG_SP,STACK+0x1f000);u.reg_write(r.UC_ARM64_REG_LR,STOP)
    endpoint=STOP if null or length!=0xc5b8 else BASE+0x19dfe04
    u.emu_start(BASE+0x19dfd58,endpoint,count=2000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==endpoint
    if endpoint==STOP:
        assert u.reg_read(r.UC_ARM64_REG_X0)==0xc000000d and not helpers
        assert bytes(u.mem_read(OUTPUT,0xc5b8))==b'\xa5'*0xc5b8
    else:
        assert helpers==['zero-private-tail','other-private-field-builder']
        assert bytes(u.mem_read(OUTPUT+0xec,8))==struct.pack('<II',word,word^0xffffffff)
        assert bytes(u.mem_read(OUTPUT,4))==b'ADVN'
        assert struct.unpack('<I',u.mem_read(OUTPUT+0x10,4))[0]==1
    return dict(source_word=word,length=length,null=null,prefix_only=endpoint!=STOP)


def main():
    path=ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==KMD_HASH
    pe=pefile.PE(str(path),fast_load=True)
    assert 0x19d9ad4+4*struct.unpack('<i',pe.get_data(0x19d9cc4,4))[0]==0x19d9a8c
    assert pe.get_data(0x19d9a94,4)==bytes.fromhex('b1180094')
    cases=[run(pe,w) for w in (0,0x23,0x25,0x26,0xabcd0023,0xffffffff)]
    cases += [run(pe,0x23,length=n) for n in (0,0xc5b7,0xc5b9)]
    cases.append(run(pe,0x23,null=True))
    print(json.dumps(dict(passed=True,hardware_access=False,producer='0x19dfd58',
                         source='object+0x4648',destination='private+0xec',cases=cases,
                         limitations=['Success executes only through 0x19dfe04; remaining producer code is not executed',
                                      'Other private-field builder and memset are modeled',
                                      'Actual N1X object value and physical adapter-to-dispatch binding remain unresolved']),indent=2))


if __name__=='__main__':main()
