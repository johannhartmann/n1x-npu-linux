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


def source_query(pe, first, second, success, concrete=False):
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for page in (0x19dd000,0xd57000,0xcd000,0xdd1000,0xdf8000,0xce000,
                 0xd3a000,0xd3f000,0x93000,0x1512000,0xe3e000):
        u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
    for address in (OBJ,QUERY,OUTPUT,STACK,STOP):u.mem_map(address,0x20000)
    u.mem_write(OBJ+0x52f8,struct.pack('<Q',QUERY))
    u.mem_write(OBJ+0x4648,struct.pack('<II',0x777,0x888))
    u.mem_write(BASE+0x1512148,struct.pack('<Q',QUERY))
    u.mem_write(OUTPUT+0x58,struct.pack('<Q',BASE+0xd3fa00))
    u.mem_write(BASE+0xe3eaf0,struct.pack('<Q',STOP+0x60))
    calls=[]
    transport_return=[]
    def hook(_,address,size,user):
        if address==BASE+0xcd148:
            assert u.reg_read(r.UC_ARM64_REG_X2)==8
            u.mem_write(u.reg_read(r.UC_ARM64_REG_X0),bytes(8))
        elif address==BASE+0xdd1a30:
            args=[u.reg_read(getattr(r,f'UC_ARM64_REG_X{i}')) for i in range(7)]
            assert args[0]==QUERY and args[1]==0x15800006 and args[3]==8 and args[5]==0
            assert bytes(u.mem_read(args[4],40))==struct.pack('<II',1,1)+bytes(32)
            assert bytes(u.mem_read(args[2],8))==bytes(8)
            calls.append(args[1])
            if concrete:
                transport_return.append(u.reg_read(r.UC_ARM64_REG_LR))
                u.reg_write(r.UC_ARM64_REG_X1,args[2])
                u.reg_write(r.UC_ARM64_REG_LR,STOP+0x40)
                u.reg_write(r.UC_ARM64_REG_PC,BASE+0xd3a4a0)
                return
            u.mem_write(args[2],struct.pack('<II',first,second))
            u.mem_write(args[6],struct.pack('<I',0 if success else 0x65))
            u.reg_write(r.UC_ARM64_REG_X0,int(success))
        elif address==BASE+0x93630:
            assert concrete and u.reg_read(r.UC_ARM64_REG_X0)==QUERY
            u.reg_write(r.UC_ARM64_REG_X0,OUTPUT)
        elif address==STOP+0x40:
            assert u.reg_read(r.UC_ARM64_REG_X0)==0
            u.reg_write(r.UC_ARM64_REG_X0,1)
            u.reg_write(r.UC_ARM64_REG_PC,transport_return.pop())
            return
        elif address==STOP+0x60:pass
        elif address not in (BASE+0xd57b60,BASE+0xd57b80,BASE+0xdf8f38,BASE+0xce4b0):return
        u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    u.reg_write(r.UC_ARM64_REG_X0,OBJ)
    u.reg_write(r.UC_ARM64_REG_SP,STACK+0x1f000);u.reg_write(r.UC_ARM64_REG_LR,STOP)
    u.emu_start(BASE+0x19ddd20,STOP,count=2000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==STOP and calls==[0x15800006]
    pair=(first,second) if success else (0x777,0x888)
    assert bytes(u.mem_read(OBJ+0x4648,8))==struct.pack('<II',*pair)
    combined=(pair[1]+(pair[0]<<4))&0xffffffff
    assert struct.unpack('<II',u.mem_read(OBJ+0x3214,8))==(combined,0)
    assert u.mem_read(OBJ+0x321c,1)==b'\0'
    assert u.reg_read(r.UC_ARM64_REG_X0)==1
    return dict(first=first,second=second,modeled_success=success,combined=combined,
                concrete_handler_and_pair_getter=concrete)


def main():
    path=ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==KMD_HASH
    pe=pefile.PE(str(path),fast_load=True)
    assert 0x19d9ad4+4*struct.unpack('<i',pe.get_data(0x19d9cc4,4))[0]==0x19d9a8c
    assert pe.get_data(0x19d9a94,4)==bytes.fromhex('b1180094')
    assert struct.unpack('<Q',pe.get_data(0x108e500,8))[0]==BASE+0xd3a4a0
    assert struct.unpack('<II',pe.get_data(0x108e510,8))==(0x15800006,8)
    cases=[run(pe,w) for w in (0,0x23,0x25,0x26,0xabcd0023,0xffffffff)]
    cases += [run(pe,0x23,length=n) for n in (0,0xc5b7,0xc5b9)]
    cases.append(run(pe,0x23,null=True))
    source_cases=[source_query(pe,a,b,s) for a,b,s in
                  [(0x23,0,True),(0x25,1,True),(0x26,0xf,True),
                   (0xffffffff,0xffffffff,True),(0x23,0,False)]]
    source_cases.append(source_query(pe,0xe0000025,4,True,concrete=True))
    routes=[]
    for kind, subtype in [(k,s) for k in (0,1,4,5,6) for s in (0,1,2,3)]+[(None,None)]:
        u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        for page in (0x19b0000,0x19b1000,0xdca000,0x19d9000,0x1a76000):
            u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
        for address in (OBJ,QUERY,STACK,STOP):u.mem_map(address,0x20000)
        if kind is not None:u.mem_write(OBJ+0x10,struct.pack('<II',kind,subtype))
        alternate=(kind,subtype)==(5,2)
        endpoint=BASE+(0x19d9a28 if alternate else 0x1a768f0)
        u.reg_write(r.UC_ARM64_REG_X1,0 if kind is None else OBJ)
        u.reg_write(r.UC_ARM64_REG_X2,QUERY)
        u.reg_write(r.UC_ARM64_REG_SP,STACK+0x1f000);u.reg_write(r.UC_ARM64_REG_LR,STOP)
        u.emu_start(BASE+0x19b0fd8,endpoint,count=1000)
        assert u.reg_read(r.UC_ARM64_REG_PC)==endpoint
        selected=alternate or (kind in (1,4) and subtype==1)
        assert u.reg_read(r.UC_ARM64_REG_X0)==(OBJ if selected else 0)
        assert u.reg_read(r.UC_ARM64_REG_X1)==QUERY
        routes.append(dict(kind=kind,subtype=subtype,alternate=alternate,valid_object=selected))
    print(json.dumps(dict(passed=True,hardware_access=False,producer='0x19dfd58',
                         source='object+0x4648',destination='private+0xec',cases=cases,router_cases=routes,
                         source_query_cases=source_cases,
                         limitations=['Success executes only through 0x19dfe04; remaining producer code is not executed',
                                      'Other private-field builder and memset are modeled',
                                      'Five control responses are modeled; one uses original handler/getter through a transport shim',
                                      'Full generic method lookup/marshalling and live Windows invocation are not executed',
                                      'Kind/subtype meaning, actual N1X object value and OS callback binding remain unresolved']),indent=2))


if __name__=='__main__':main()
