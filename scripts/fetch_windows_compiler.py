#!/usr/bin/env python3
"""Fetch only the Windows ARM64 compiler ZIP entry from the official SDK.

Validates range sizes, ZIP CRC and the pinned extracted-file SHA-256.
Does not execute or install the acquired DLL. Run from the repository root.
"""
import urllib.request,struct,json,zlib,hashlib
from pathlib import Path
url='https://developer.download.nvidia.com/compute/machine-learning/tensorrt/11.4.0/zip/TensorRT-Enterprise-11.4.0.106-Windows-arm64-cuda-13.4-Release-external.zip'
def get(a,b):
    req=urllib.request.Request(url,headers={'Range':f'bytes={a}-{b}'})
    with urllib.request.urlopen(req,timeout=45) as r:
        assert r.status==206,(r.status,r.headers)
        data=r.read();assert len(data)==b-a+1;return data
Path('results').mkdir(exist_ok=True)
size=332016940
tail=get(size-65536,size-1); i=tail.rfind(b'PK\x05\x06');assert i>=0
_,disk,cdisk,n1,n2,cs,co,comment=struct.unpack_from('<4s4H2IH',tail,i)
assert disk==cdisk==0 and n1==n2
cd=get(co,co+cs-1);p=0;entries=[]
while p<len(cd):
    h=struct.unpack_from('<4s6H3I5H2I',cd,p);assert h[0]==b'PK\x01\x02'
    nl,xl,cl=h[10:13];name=cd[p+46:p+46+nl].decode();entries.append(dict(name=name,method=h[4],crc=h[7],compressed=h[8],size=h[9],offset=h[-1]));p+=46+nl+xl+cl
Path('results/tensorrt-sdk-inventory.json').write_text(json.dumps(dict(url=url,size=size,entries=entries),indent=2))
for e in entries:
    if 'dla' in e['name'].lower():print(e,flush=True)
for e in entries:
    if e['name'].lower().endswith('nvdla_compiler.dll'):
        h=get(e['offset'],e['offset']+29);fields=struct.unpack('<4s5H3I2H',h);start=e['offset']+30+fields[-2]+fields[-1]
        compressed=get(start,start+e['compressed']-1)
        raw=zlib.decompress(compressed,-15) if e['method']==8 else compressed
        assert len(raw)==e['size'] and zlib.crc32(raw)==e['crc']
        assert hashlib.sha256(raw).hexdigest()=='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
        target=Path('sources/tensorrt-11.4/nvdla_compiler.dll');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);print('EXTRACTED',target,len(raw),hashlib.sha256(raw).hexdigest(),flush=True)
