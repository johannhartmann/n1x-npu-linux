#!/usr/bin/env python3
"""List PE resource payload ranges without reading large payloads into RAM."""
import argparse,json,mmap,struct
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('pe',type=Path);a=p.parse_args()
with a.pe.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as b:
 pe=struct.unpack_from('<I',b,0x3c)[0];assert b[pe:pe+4]==b'PE\0\0'
 machine,n=struct.unpack_from('<HH',b,pe+4);optsize=struct.unpack_from('<H',b,pe+20)[0];opt=pe+24;magic=struct.unpack_from('<H',b,opt)[0];assert magic==0x20b
 rva,size=struct.unpack_from('<II',b,opt+112+2*8);sections=[]
 for i in range(n):
  at=opt+optsize+40*i;vsize,va,rawsize,raw=struct.unpack_from('<IIII',b,at+8);sections.append((va,max(vsize,rawsize),raw))
 def offset(va):
  for base,length,raw in sections:
   if base<=va<base+length:return raw+va-base
  raise ValueError(hex(va))
 root=offset(rva);rows=[]
 def walk(rel,names,depth=0):
  if depth>4:raise ValueError('resource nesting too deep')
  named,ids=struct.unpack_from('<HH',b,root+rel+12)
  for i in range(named+ids):
   name,target=struct.unpack_from('<II',b,root+rel+16+i*8)
   if name&0x80000000:
    at=root+(name&0x7fffffff);length=struct.unpack_from('<H',b,at)[0];name=b[at+2:at+2+length*2].decode('utf-16le')
   if target&0x80000000:walk(target&0x7fffffff,names+[name],depth+1)
   else:
    va,length,codepage,_=struct.unpack_from('<IIII',b,root+target);off=offset(va);rows.append(dict(path=names+[name],rva=hex(va),offset=off,size=length,magic=b[off:off+16].hex()))
 walk(0,[])
 print(json.dumps(dict(machine=hex(machine),resources=rows),indent=2))
