#!/usr/bin/env python3
"""Extract a hash-pinned T35x DLA research image from NVIDIA's Windows ARM64 KMD."""
import argparse,hashlib,json,struct
from pathlib import Path
import pefile
KMD_SHA256='73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987'


def parse_header(data):
    if len(data)<24:raise ValueError('truncated outer header')
    magic,version,size,header,offset,length=struct.unpack_from('<6I',data)
    if magic!=0x10fe or version!=1:raise ValueError('unsupported magic/version')
    if size!=len(data):raise ValueError('image size mismatch')
    if header<24 or header+0x4c>size:raise ValueError('extended OS header out of bounds')
    if offset<header+0x4c or offset+length>size:raise ValueError('OS payload out of bounds')
    data_offset,data_size,code_offset,code_size=struct.unpack_from('<4I',data,header+0x28)
    for start,count in ((code_offset,code_size),(data_offset,data_size)):
        if start+count>length:raise ValueError('section out of bounds')
    if code_size and data_size and max(code_offset,data_offset)<min(code_offset+code_size,data_offset+data_size):raise ValueError('overlapping code/data')
    return dict(magic=hex(magic),version=version,size=size,os_header_offset=header,
        payload_offset=offset,payload_size=length,code_offset=code_offset,
        code_size=code_size,data_offset=data_offset,data_size=data_size)


def extract(kmd,output):
    digest=hashlib.file_digest(kmd.open('rb'),'sha256').hexdigest()
    if digest!=KMD_SHA256:raise ValueError('unsupported KMD hash')
    pe=pefile.PE(str(kmd),fast_load=True)
    if pe.FILE_HEADER.Machine!=0xaa64:raise ValueError('expected ARM64 KMD')
    if pe.get_data(0xd4a9a0,8)!=bytes.fromhex('281a00b008013691'):raise ValueError('descriptor getter changed')
    tag,va,size=struct.unpack('<QQQ',pe.get_data(0x108fd80,24));rva=va-pe.OPTIONAL_HEADER.ImageBase
    blob=pe.get_data(rva,size)
    header=parse_header(blob)
    with output.open('xb') as f:f.write(blob)
    return dict(source_sha256=digest,descriptor_rva='0x108fd80',descriptor_tag=hex(tag),
        payload_rva=hex(rva),payload_file_offset=hex(pe.get_offset_from_rva(rva)),
        payload_sha256=hashlib.sha256(blob).hexdigest(),header=header)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('kmd',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    print(json.dumps(extract(a.kmd,a.output),indent=2))
