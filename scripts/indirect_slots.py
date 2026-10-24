#!/usr/bin/env python3
"""Candidate vtable/dispatch slot calls, requiring adjacent matching BLR/BR."""
import argparse,bisect,json,struct
from pathlib import Path
import pefile
p=argparse.ArgumentParser();p.add_argument('binary',type=Path);p.add_argument('slots',nargs='+',type=lambda x:int(x,0));a=p.parse_args();pe=pefile.PE(str(a.binary),fast_load=True);b=pe.__data__;base=pe.OPTIONAL_HEADER.ImageBase
pdata=next(s for s in pe.sections if s.Name.rstrip(b'\0')==b'.pdata').get_data();starts=sorted({struct.unpack_from('<I',pdata,o)[0] for o in range(0,len(pdata)-7,8)});rows=[]
for s in pe.sections:
 if not s.Characteristics&0x20000000:continue
 lo=s.PointerToRawData;hi=lo+s.SizeOfRawData
 for o in range(lo,hi-48,4):
  ins=struct.unpack_from('<I',b,o)[0]
  if ins&0xffc00000!=0xf9400000 or ((ins>>10)&4095)*8 not in a.slots:continue
  reg=ins&31; aliases={reg}
  for delta in range(4,49,4):
   nxt=struct.unpack_from('<I',b,o+delta)[0]
   if nxt&0xffe0ffe0==0xaa0003e0 and (nxt>>16)&31 in aliases: aliases.add(nxt&31)
   if nxt&0xfffffc1f in (0xd63f0000,0xd61f0000) and (nxt>>5)&31 in aliases:
    rva=s.VirtualAddress+o-lo;rows.append({'slot':hex(((ins>>10)&4095)*8),'load_va':hex(base+rva),'call_va':hex(base+rva+delta),'function_va':hex(base+starts[bisect.bisect_right(starts,rva)-1])});break
print(json.dumps(rows,indent=2))
