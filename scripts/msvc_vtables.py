#!/usr/bin/env python3
import argparse,json,struct
from pathlib import Path
import pefile
p=argparse.ArgumentParser();p.add_argument('binary',type=Path);a=p.parse_args();pe=pefile.PE(str(a.binary),fast_load=True);b=pe.__data__;base=pe.OPTIONAL_HEADER.ImageBase
code=[(s.VirtualAddress,s.VirtualAddress+s.Misc_VirtualSize) for s in pe.sections if s.Characteristics&0x20000000]
rows=[];start=0
while (o:=b.find(b'.?AV',start))>=0:
 start=o+4;end=b.find(b'\0',o);name=b[o:end].decode(errors='replace');type_rva=pe.get_rva_from_offset(o-16);cursor=0
 while (at:=b.find(struct.pack('<I',type_rva),cursor))>=0:
  cursor=at+4
  if at<12 or at+12>len(b):continue
  sig,offset,cd,typ,hier,selfrva=struct.unpack_from('<6I',b,at-12)
  if sig!=1 or selfrva!=pe.get_rva_from_offset(at-12):continue
  col=base+selfrva;vstart=0
  while (vp:=b.find(struct.pack('<Q',col),vstart))>=0:
   vstart=vp+8;entries=[]
   for slot in range(256):
    pos=vp+8+slot*8
    if pos+8>len(b):break
    fn=struct.unpack_from('<Q',b,pos)[0]
    if not any(lo<=fn-base<hi for lo,hi in code):break
    entries.append({'slot':hex(slot*8),'va':hex(fn)})
   if entries:rows.append({'class':name,'object_offset':offset,'vtable_va':hex(base+pe.get_rva_from_offset(vp+8)),'methods':entries})
print(json.dumps(rows,indent=2))
