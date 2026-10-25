#!/usr/bin/env python3
"""Run original content-vector materialization with modeled C++ containers."""
import argparse
import hashlib
import json
import struct
import re
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_runtime_selector import ROOT,BASE,RUNTIME_HASH
from inspect_loadable import Buffer
from diagnostic_fixture import DIAGNOSTIC_HASH
from verify_relu_loadable import LOADABLE_HASH

CTX,MODEL,HEAP,STACK,STOP=0x40000000,0x50000000,0x60000000,0x70000000,0x71000000


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relu',action='store_true',help='Inspect the checked compiler-generated ReLU artifact')
    args=parser.parse_args()
    path=ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==RUNTIME_HASH
    pe=pefile.PE(str(path),fast_load=True)
    data=(ROOT/('results/native-compile-t254/relu.nvda' if args.relu else 'sources/diagnostic-verified.nvda')).read_bytes()
    assert hashlib.sha256(data).hexdigest()==(LOADABLE_HASH if args.relu else DIAGNOSTIC_HASH)
    blob_count=8 if args.relu else 7
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    size=(pe.OPTIONAL_HEADER.SizeOfImage+4095)&~4095
    u.mem_map(BASE,size);u.mem_write(BASE,pe.get_memory_mapped_image())
    for addr,size in ((CTX,0x10000),(MODEL,(len(data)+4095)&~4095),(HEAP,0x1000000),(STACK,0x10000),(STOP,4096)):
        u.mem_map(addr,size)
    u.mem_write(MODEL,data)
    cursor=HEAP
    nodes={}
    copies=[]
    comparisons=[]
    callbacks=[]
    pattern=pe.get_data(0x98478,128).split(b'\0')[0].decode('ascii')
    assert pattern==r'^task-?(\d+)-stat(s|_list)$'
    def reg(i):return u.reg_read(getattr(r,f'UC_ARM64_REG_X{i}'))
    def ret(value=None):
        if value is not None:u.reg_write(r.UC_ARM64_REG_X0,value)
        u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
    def alloc(n):
        nonlocal cursor
        ptr=cursor;cursor=(cursor+max(n,1)+15)&~15
        assert cursor<HEAP+0x1000000
        return ptr
    def string(ptr):
        length,capacity=struct.unpack('<QQ',u.mem_read(ptr+16,16))
        start=struct.unpack('<Q',u.mem_read(ptr,8))[0] if capacity>=16 else ptr
        return bytes(u.mem_read(start,length))
    def assign(ptr,value):
        u.mem_write(ptr,bytes(32))
        if len(value)<16:
            u.mem_write(ptr,value+b'\0');capacity=15
        else:
            backing=alloc(len(value)+1);u.mem_write(backing,value+b'\0')
            u.mem_write(ptr,struct.pack('<Q',backing));capacity=len(value)
        u.mem_write(ptr+16,struct.pack('<QQ',len(value),capacity))
    def hook(_,address,size,user):
        # Keep the original stack guards: they reserve/release 16 stack bytes.
        if address==BASE+0x8c5e8:
            length=struct.unpack('<I',u.mem_read(reg(0),4))[0]
            assign(reg(1),bytes(u.mem_read(reg(0)+4,length)));ret(reg(1))
        elif address==BASE+0x7cfb0:
            assert reg(0)==CTX+0xd0
            name=string(reg(2)).decode()
            if name not in nodes:nodes[name]=alloc(128)
            u.mem_write(reg(1),struct.pack('<Q',nodes[name]));ret(reg(1))
        elif address==BASE+0x7c870:
            assert reg(0)==CTX+0xd0
            ret(nodes.get(string(reg(1)).decode(),0))
        elif address in (BASE+0x6b290,BASE+0x6b2d8):
            assert reg(0)==CTX+8
            ret(0)
        elif address==BASE+0x16418:
            assign(reg(0),bytes(u.mem_read(reg(1),reg(2))));ret(reg(0))
        elif address==BASE+0x7d970:
            assert reg(1)==BASE+0x6a5b10
            name=string(reg(0)).decode('ascii')
            match=re.search(pattern,name,flags=re.ASCII) is not None
            comparisons.append(dict(name=name,match=match));ret(int(match))
        elif address==BASE+0x1b280:
            # Observe entry but execute the actual two-instruction callback.
            callbacks.append(True)
        elif address==BASE+0x904b0:ret(alloc(reg(0)))
        elif address==BASE+0x1820:
            u.mem_write(reg(0),bytes([reg(1)&255])*reg(2));ret(reg(0))
        elif address==BASE+0x1100:
            copied=bytes(u.mem_read(reg(1),reg(2)))
            u.mem_write(reg(0),copied);copies.append((reg(0),len(copied)));ret(reg(0))
        elif address==BASE+0x4bb68:ret()
    u.hook_add(UC_HOOK_CODE,hook)
    root=struct.unpack_from('<I',data)[0]
    u.reg_write(r.UC_ARM64_REG_X0,CTX);u.reg_write(r.UC_ARM64_REG_X1,MODEL+root)
    u.reg_write(r.UC_ARM64_REG_X2,CTX+0x8000)
    u.reg_write(r.UC_ARM64_REG_SP,STACK+0xf000);u.reg_write(r.UC_ARM64_REG_LR,STOP)
    u.emu_start(BASE+0x88160,STOP,count=100000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==STOP and reg(0)==0
    assert len(copies)==blob_count
    view=Buffer(data);rows=[]
    for table in view.tables(root,5):
        name=view.string(table,0);node=nodes[name]
        size=struct.unpack('<Q',u.mem_read(node+0x70,8))[0]
        pointer=struct.unpack('<Q',u.mem_read(node+0x78,8))[0]
        extent=view.vector(table,5,1);expected=data[extent.start:extent.stop]
        assert size==len(expected)==view.scalar(table,1,'<Q')
        assert bytes(u.mem_read(pointer,size))==expected and (pointer,size) in copies
        version_field=view.field(table,4)
        assert bytes(u.mem_read(node+0x68,3))==data[version_field:version_field+3]
        assert struct.unpack('<II',u.mem_read(node+0x60,8))==(view.scalar(table,2,'<I'),view.scalar(table,3,'<I'))
        assign(CTX+0x1000,name.encode())
        u.mem_write(CTX+0x2000,bytes(0x40))
        for i,value in enumerate((CTX,CTX+0x1000,CTX+0x2000,CTX+0x3000)):
            u.reg_write(getattr(r,f'UC_ARM64_REG_X{i}'),value)
        u.reg_write(r.UC_ARM64_REG_SP,STACK+0xf000);u.reg_write(r.UC_ARM64_REG_LR,STOP)
        u.emu_start(BASE+0x8b350,STOP,count=10000)
        assert u.reg_read(r.UC_ARM64_REG_PC)==STOP and reg(0)==1
        assert string(CTX+0x2000)==name.encode()
        assert struct.unpack('<Q',u.mem_read(CTX+0x2020,8))[0]==size
        assert bytes(u.mem_read(CTX+0x2028,8))==bytes(u.mem_read(node+0x60,8))
        assert bytes(u.mem_read(CTX+0x2030,6))==bytes(u.mem_read(node+0x68,6))
        assert struct.unpack('<Q',u.mem_read(CTX+0x3000,8))[0]==pointer
        # Original normal-loader copy and actual 0x1b280 callback, starting
        # after prior compatibility checks. Exercise bounds before copying.
        destination=alloc(size+32)
        u.mem_write(destination,b'\xa5'*(size+32))
        u.mem_write(CTX+0x5000+0x50,bytes(u.mem_read(CTX+0x2000,0x36)))
        u.mem_write(CTX+0x5010,struct.pack('<Q',pointer))
        u.mem_write(CTX+0x4000,struct.pack('<Q',CTX))
        u.mem_write(CTX+0x4060,struct.pack('<Q',destination))
        for allocation_size in (size-1,size):
            u.mem_write(CTX+0x4018,struct.pack('<Q',allocation_size))
            u.reg_write(r.UC_ARM64_REG_X21,CTX+0x4000)
            u.reg_write(r.UC_ARM64_REG_X23,CTX+0x6000)
            u.reg_write(r.UC_ARM64_REG_X26,CTX+0x5000)
            u.reg_write(r.UC_ARM64_REG_SP,STACK+0xf000)
            endpoint=BASE+(0x21018 if allocation_size<size else 0x20f5c)
            before=len(copies)
            u.emu_start(BASE+0x20f18,endpoint,count=1000)
            assert u.reg_read(r.UC_ARM64_REG_PC)==endpoint
            if allocation_size<size:
                assert len(copies)==before and bytes(u.mem_read(destination,size+32))==b'\xa5'*(size+32)
            else:
                assert len(copies)==before+1 and u.reg_read(r.UC_ARM64_REG_W19)==0
                assert bytes(u.mem_read(destination,size))==expected
                assert bytes(u.mem_read(destination+size,32))==b'\xa5'*32
        rows.append(dict(name=name,bytes=size,version=list(u.mem_read(node+0x68,3)),
                         interface=struct.unpack('<I',u.mem_read(node+0x60,4))[0],original_content_getter_verified=True,
                         sub_interface=struct.unpack('<I',u.mem_read(node+0x64,4))[0],
                         original_loader_copy_and_bounds_verified=True,
                         sha256=hashlib.sha256(expected).hexdigest()))
    assert len(rows)==len(nodes)==blob_count and len(copies)==2*blob_count and len(callbacks)==blob_count
    assert len(comparisons)==blob_count
    assert [row['name'] for row in comparisons if row['match']]==(['task-0-stats'] if args.relu else [])
    if args.relu:
        assert next(row for row in rows if row['name']=='task-0-nw')['bytes']==256
        assert next(row for row in rows if row['name']=='task-0-stats')['sub_interface']==8
        assert all(row['version']==([3,0,0] if row['interface']==1 else [0,0,0]) for row in rows)
    else:assert next(row for row in rows if row['name']=='task0-addr0')['bytes']==128
    print(json.dumps(dict(passed=True,hardware_access=False,original_function='0x88160',contents=rows,
                         loadable_sha256=hashlib.sha256(data).hexdigest(),fixture='relu' if args.relu else 'legacy diagnostic',
                         special_symbol_pattern=pattern,special_symbol_comparisons=comparisons,
                         limitations=['C++ map/string/allocation/copy helpers are modeled; original field extraction and control flow execute',
                                      'Recovered regex is matched by Python over ASCII names; original MSVC regex compiler/executor is not run',
                                      'Normal-loader copy slice begins after compatibility checks and stops before cleanup/next blob',
                                      'This is content-vector materialization only, not full deserialization, load or device execution',
                                      'Later runtime transforms or a different model/firmware ABI remain possible']),indent=2))


if __name__=='__main__':main()
