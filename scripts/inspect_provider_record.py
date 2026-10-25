#!/usr/bin/env python3
"""Audit provider record copying and the original adapter-query ABI wrapper."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH, CTX, OUT, STACK, STOP, call


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(path), fast_load=True)
    # Symbol binding: loader context is the global table base, +0xe8 is
    # exactly the function-pointer slot read by the query wrapper.
    assert pe.get_data(0x981f8, 22).split(b'\0')[0] == b'D3DKMTQueryAdapterInfo'
    assert pe.get_data(0x78020, 8) == bytes.fromhex('683100b013c12591')
    assert pe.get_data(0x782d4, 28) == bytes.fromhex('0801009001e10791600640f9680100b0088140f900013fd6607600f9')
    rows = []
    for index in (0, 1):
        u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for page in (0x79000, 0x1000, 0x6a5000):
            u.mem_map(BASE+page, 4096)
            u.mem_write(BASE+page, pe.get_data(page, 4096))
        for addr in (CTX, OUT, STACK, STOP): u.mem_map(addr, 0x10000)
        source_base = 0x40000000
        u.mem_map(source_base, 0x20000)
        data = bytes((i*37+index*19)&255 for i in range(0xc5e8))
        source = source_base+index*0xc5e8
        u.mem_write(source, data)
        u.mem_write(BASE+0x6a5ae0, struct.pack('<QQQ', source_base, source_base+2*0xc5e8, source_base+2*0xc5e8))
        def hook(_, address, size, user):
            if address not in (BASE+0x1100, BASE+0x1820): return
            dest, arg, length = [u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}')) for i in range(3)]
            u.mem_write(dest, bytes(u.mem_read(arg, length)) if address == BASE+0x1100 else bytes([arg&255])*length)
            u.reg_write(r.UC_ARM64_REG_X0, dest)
            u.reg_write(r.UC_ARM64_REG_PC, u.reg_read(r.UC_ARM64_REG_LR))
        u.hook_add(UC_HOOK_CODE, hook)
        assert call(u, 0x791e0, CTX, index) == CTX
        assert bytes(u.mem_read(CTX+8, len(data))) == data
        assert struct.unpack('<I', u.mem_read(CTX,4))[0] == 0x11a9a
        assert bytes(u.mem_read(CTX+0x124,4)) == data[0x11c:0x120]
        rows.append({'index': index, 'full_record_copy_verified': True})
    for query_type, length in ((0,0xc5b8),(13,4),(15,4),(31,28)):
        u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for page in (0x78000,0x6a5000,0xa5000):
            u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
        for addr in (CTX,OUT,STACK,STOP): u.mem_map(addr,0x10000)
        u.mem_write(BASE+0x6a5a58,struct.pack('<Q',STOP+0x20))
        u.mem_write(BASE+0xa5340,struct.pack('<Q',STOP+0x40))
        captured=[]
        def hook(_,address,size,user):
            if address==STOP+0x40:
                u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
            elif address==STOP+0x20:
                ptr=u.reg_read(r.UC_ARM64_REG_X0)
                fields=struct.unpack('<IIQII',u.mem_read(ptr,24))
                assert fields==(0x1234,query_type,OUT,length,0)
                captured.append(fields)
                u.reg_write(r.UC_ARM64_REG_X0,0xc0000001)
                u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
        u.hook_add(UC_HOOK_CODE,hook)
        assert call(u,0x786b0,0x1234,query_type,length,OUT)==0xc0000001
        assert len(captured)==1
        rows.append({'query_type':query_type,'length':length,'original_wrapper_verified':True})
    print(json.dumps(dict(passed=True,hardware_access=False,cases=rows,
                         private_field_offset='0xec',record_field_offset='0x11c',backing_field_offset='0x124',
                         limitations=['Enumeration and kernel response contents are not executed or observed',
                                      'Private field semantic name and actual N1X value remain unresolved']),indent=2))


if __name__=='__main__':main()
