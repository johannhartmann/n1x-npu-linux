#!/usr/bin/env python3
"""Compare native compiler version bytes with the original Windows requirement.

Executes only leaf/compare instructions under Unicorn, never dlopen or hardware.
"""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM
from unicorn import arm64_const as r

ROOT=Path(__file__).resolve().parents[1]
NATIVE_HASH='28668e74ced3c04ca34f30d41b7ea1d39cabe791418fbad31fcfb009d5558ad9'
WINDOWS_HASH='36b2cf0523259d4602037c120a793739fdd6068bad86d73b596453817c75de3f'
BASE,OUT,STOP=0x180000000,0x40000000,0x41000000


def main():
    directory=ROOT/'sources/tensorrt-11.4'
    elf=(directory/'native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so').read_bytes()
    assert hashlib.sha256(elf).hexdigest()==NATIVE_HASH
    assert elf[:6]==b'\x7fELF\x02\x01' and struct.unpack_from('<H',elf,18)[0]==183
    phoff=struct.unpack_from('<Q',elf,32)[0]
    phsize,count=struct.unpack_from('<HH',elf,54)
    segments=[struct.unpack_from('<IIQQQQQQ',elf,phoff+i*phsize) for i in range(count)]
    def read(address,size):
        for kind,flags,offset,vaddr,paddr,filesz,memsz,align in segments:
            if kind==1 and vaddr<=address and address+size<=vaddr+filesz:
                return elf[offset+address-vaddr:offset+address-vaddr+size]
        raise ValueError('unbacked ELF address')
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for page in (0x621000,0xa3d000,0xa74000):
        u.mem_map(page,4096);u.mem_write(page,read(page,4096))
    u.mem_map(OUT,4096);u.mem_map(STOP,4096)
    u.mem_write(OUT,b'\xa5'*64)
    u.reg_write(r.UC_ARM64_REG_X0,OUT);u.reg_write(r.UC_ARM64_REG_LR,STOP)
    u.emu_start(0x6217b0,STOP,count=100)
    assert u.reg_read(r.UC_ARM64_REG_PC)==STOP and u.reg_read(r.UC_ARM64_REG_X0)==0
    version=struct.unpack('<4Q',u.mem_read(OUT,32))
    assert version==(1,3,16,13) and bytes(u.mem_read(OUT+32,32))==b'\xa5'*32
    u.reg_write(r.UC_ARM64_REG_X0,0);u.reg_write(r.UC_ARM64_REG_LR,STOP)
    u.emu_start(0x6217b0,STOP,count=100)
    assert u.reg_read(r.UC_ARM64_REG_X0)==4
    path=directory/'nvinfer_11.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==WINDOWS_HASH
    pe=pefile.PE(str(path),fast_load=True)
    cases=[]
    for fields,expected in [(version,0),((1,3,16,12),255),((1,3,16,14),1),
                            ((1,3,15,999),255),((1,4,0,0),1),((0,99,99,99),255),((2,0,0,0),1)]:
        w=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        for page in (0x725000,0x726000):
            w.mem_map(BASE+page,4096);w.mem_write(BASE+page,pe.get_data(page,4096))
        w.mem_map(OUT,4096);w.mem_write(OUT+0x10,struct.pack('<4Q',*fields))
        w.reg_write(r.UC_ARM64_REG_X22,OUT);w.reg_write(r.UC_ARM64_REG_X10,1)
        w.emu_start(BASE+0x725fb4,BASE+0x725fcc,count=100)
        assert w.reg_read(r.UC_ARM64_REG_PC)==BASE+0x725fcc
        result=w.reg_read(r.UC_ARM64_REG_W8)&255
        assert result==expected
        cases.append(dict(version=list(fields),comparison=result))
    print(json.dumps(dict(passed=True,hardware_access=False,native_sha256=NATIVE_HASH,
                         windows_sha256=WINDOWS_HASH,native_version_fields=list(version),
                         windows_compare_cases=cases,
                         limitations=['Leaf version getter and comparison slice only; no compiler or network build is executed',
                                      'Matching version bytes do not establish every C API layout, target support or firmware ABI',
                                      'Native constructors, Windows loader and OS transport are not executed']),indent=2))


if __name__=='__main__':main()
