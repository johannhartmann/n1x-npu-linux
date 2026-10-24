#!/usr/bin/env python3
"""Find candidate ARM64 ADRP+ADD references to selected PE strings offline."""
import argparse,json,mmap,struct
from pathlib import Path
import pefile
p=argparse.ArgumentParser();p.add_argument('pe',type=Path);p.add_argument('strings',nargs='+');a=p.parse_args();pe=pefile.PE(str(a.pe),fast_load=True);base=pe.OPTIONAL_HEADER.ImageBase
with a.pe.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as b:
 targets={}
 for name in a.strings:
  if name.startswith('@'):
   targets[int(name[1:],0)]=name;continue
  at=b.find(name.encode()+b'\0')
  if at<0:raise ValueError('missing '+name)
  targets[base+pe.get_rva_from_offset(at)]=name
 rows=[];pages={t&~4095 for t in targets}
 for sec in pe.sections:
  if not sec.Characteristics&0x20000000:continue
  start=sec.PointerToRawData;end=start+sec.SizeOfRawData
  for off in range(start,end-4,4):
   ins=struct.unpack_from('<I',b,off)[0]
   if ins&0x9f000000!=0x90000000:continue
   imm=((ins>>29)&3)|(((ins>>5)&0x7ffff)<<2)
   if imm&(1<<20):imm-=1<<21
   pc=base+sec.VirtualAddress+off-start;page=(pc&~4095)+(imm<<12)
   if page not in pages:continue
   reg=ins&31
   for delta in range(4,49,4):
    if off+delta+4>end:break
    nxt=struct.unpack_from('<I',b,off+delta)[0]
    if nxt&0xffc00000 in (0x91000000,0xf9400000) and (nxt>>5)&31==reg:
     scale=8 if nxt&0xffc00000==0xf9400000 else 1
     target=page+((nxt>>10)&4095)*scale
     if target in targets:rows.append(dict(string=targets[target],string_va=hex(target),adrp_va=hex(pc),add_va=hex(pc+delta),register=reg))
 print(json.dumps(rows,indent=2))
