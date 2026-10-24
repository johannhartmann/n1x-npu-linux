#!/usr/bin/env python3
"""Extract the built-in diagnostic NVDA FlatBuffer; never submit it to hardware."""
import argparse,hashlib,json
from pathlib import Path
import pefile
from inspect_loadable import inspect
SHA256='c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b'
p=argparse.ArgumentParser(description=__doc__);p.add_argument('runtime',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
if hashlib.file_digest(a.runtime.open('rb'),'sha256').hexdigest()!=SHA256:raise ValueError('unsupported runtime hash')
pe=pefile.PE(str(a.runtime),fast_load=True)
if pe.FILE_HEADER.Machine!=0xaa64:raise ValueError('expected ARM64 runtime')
if pe.get_data(0x15f38,8)!=bytes.fromhex('02f993d2420ba0f2'):raise ValueError('loadable size argument changed')
if pe.get_data(0x15f40,8)!=bytes.fromhex('880400b001411891'):raise ValueError('loadable pointer argument changed')
blob=pe.get_data(0xa6610,0x5a9fc8)
if len(blob)!=0x5a9fc8:raise ValueError('truncated loadable')
projection=inspect(blob)
with a.output.open('xb') as f:f.write(blob)
print(json.dumps({'source_sha256':SHA256,'loadable_rva':'0xa6610',
    'loadable_file_offset':hex(pe.get_offset_from_rva(0xa6610)),
    'loadable_size':len(blob),'projection':projection},indent=2))
