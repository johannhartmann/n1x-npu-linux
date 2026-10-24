#!/usr/bin/env python3
"""Execute only the original Windows ARM64 header parser in isolated Unicorn RAM."""
import hashlib,json,struct
from pathlib import Path
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
from unicorn.arm64_const import UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3,UC_ARM64_REG_LR,UC_ARM64_REG_SP
from extract_firmware import KMD_SHA256
root=Path(__file__).resolve().parents[1];path=root/'sources/nvidia/Display.Driver/nvlddmkm.sys'
assert hashlib.file_digest(path.open('rb'),'sha256').hexdigest()==KMD_SHA256
pe=pefile.PE(str(path),fast_load=True);code=pe.get_data(0xd4c640,0x1bc)
original=(root/'sources/dla-t35x-verified.bin').read_bytes()
BASE=0x20000000;OUT=0x20100000;STACK=0x20200000;STOP=0x20300000

def run(blob,null_arg=None):
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for addr,size in ((0x140d4c000,0x1000),(0x1400cd000,0x2000),(BASE,0x20000),(OUT,0x1000),(STACK,0x10000),(STOP,0x1000)):u.mem_map(addr,size)
    u.mem_write(0x140d4c640,code);u.mem_write(BASE,bytes(blob))
    for address in (0x1400ce4b0,0x1400cd148):u.mem_write(address,bytes.fromhex('c0035fd6'))
    def hook(uc,address,size,user):
        if address==0x1400cd148:
            dst=uc.reg_read(UC_ARM64_REG_X0);value=uc.reg_read(UC_ARM64_REG_X1)&255;n=uc.reg_read(UC_ARM64_REG_X2)
            assert n==24;uc.mem_write(dst,bytes([value])*n)
    u.hook_add(UC_HOOK_CODE,hook)
    args=[0,BASE,OUT,OUT+0x100]
    if null_arg is not None:args[null_arg]=0
    for reg,value in zip((UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3),args):u.reg_write(reg,value)
    u.reg_write(UC_ARM64_REG_SP,STACK+0x8000);u.reg_write(UC_ARM64_REG_LR,STOP)
    u.emu_start(0x140d4c640,STOP,count=1000)
    return {'status':u.reg_read(UC_ARM64_REG_X0),'payload_pointer':hex(struct.unpack('<Q',u.mem_read(OUT,8))[0]),'decoded_words':list(struct.unpack('<6I',u.mem_read(OUT+0x100,24)))}

def main():
    rows=[]
    r=run(original);assert r=={'status':0,'payload_pointer':hex(BASE+0x200),'decoded_words':[0,0,0,0xaf00,0xaf00,0xa400]};rows.append(dict(case='valid_pinned_image',**r))
    for name,offset,value in [('bad_magic',0,0),('header_overflow',12,0xfffffff0),('header_oob',12,len(original)),('payload_overflow',16,0xfffffff0),('payload_oob',20,len(original)),('section_overflow',0x12c,0xffffffff),('section_oob',0x12c,len(original))]:
        blob=bytearray(original);struct.pack_into('<I',blob,offset,value);r=run(blob);assert r['status']!=0;rows.append(dict(case=name,**r))
    for arg in (1,2,3):
        r=run(original,arg);assert r['status']==0x1f;rows.append(dict(case=f'null_arg_{arg}',**r))
    print(json.dumps({'scope':'offline original ARM64 parser; logging and memset stubbed; no hardware access','cases':rows,'passed':len(rows)},indent=2))


if __name__ == "__main__":
    main()
