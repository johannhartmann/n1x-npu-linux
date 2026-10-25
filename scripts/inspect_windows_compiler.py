#!/usr/bin/env python3
"""Pin the Windows ARM compiler and execute only its leaf version getter."""
import hashlib,json,struct
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT


def main():
    path=ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest=='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
    pe=pefile.PE(str(path));assert pe.FILE_HEADER.Machine==0xaa64
    exports={x.name.decode():x.address for x in pe.DIRECTORY_ENTRY_EXPORT.symbols if x.name}
    address=exports['NvDlaCompilerGetSemVersion'];assert address==0x4b2290
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    base=pe.OPTIONAL_HEADER.ImageBase;page=address&~4095
    u.mem_map(base+page,4096);u.mem_write(base+page,pe.get_data(page,4096))
    out,stop=0x40000000,0x41000000
    for p in (out,stop):u.mem_map(p,4096)
    u.mem_write(out,b'\xa5'*64)
    u.reg_write(r.UC_ARM64_REG_X0,out);u.reg_write(r.UC_ARM64_REG_LR,stop)
    u.emu_start(base+address,stop,count=100)
    assert u.reg_read(r.UC_ARM64_REG_PC)==stop and u.reg_read(r.UC_ARM64_REG_X0)==0
    fields=struct.unpack('<4Q',u.mem_read(out,32));assert fields==(1,3,16,13)
    assert bytes(u.mem_read(out+32,32))==b'\xa5'*32
    u.reg_write(r.UC_ARM64_REG_X0,0);u.reg_write(r.UC_ARM64_REG_LR,stop)
    u.emu_start(base+address,stop,count=100)
    assert u.reg_read(r.UC_ARM64_REG_PC)==stop and u.reg_read(r.UC_ARM64_REG_X0)==4
    print(json.dumps(dict(passed=True,hardware_access=False,sha256=digest,exports=len(exports),
        version_fields=list(fields),imports=[x.dll.decode() for x in pe.DIRECTORY_ENTRY_IMPORT],
        limitations=['Only the original leaf getter executes; no Windows loader or compilation',
                     'Version equality does not prove backend or firmware compatibility']),indent=2))


if __name__=='__main__':main()
