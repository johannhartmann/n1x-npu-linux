#!/usr/bin/env python3
import hashlib,json,argparse
from pathlib import Path
import pefile
p=argparse.ArgumentParser();p.add_argument('binary',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
pe=pefile.PE(str(a.binary),fast_load=True);pe.parse_data_directories(directories=[1,0,6])
imports=[{'dll':e.dll.decode(errors='replace'),'symbols':[x.name.decode(errors='replace') if x.name else f'ordinal_{x.ordinal}' for x in e.imports]} for e in getattr(pe,'DIRECTORY_ENTRY_IMPORT',[])]
exports=[{'name':x.name.decode(errors='replace') if x.name else None,'rva':hex(x.address),'ordinal':x.ordinal} for x in getattr(getattr(pe,'DIRECTORY_ENTRY_EXPORT',None),'symbols',[])]
meta={'file':a.binary.name,'sha256':hashlib.file_digest(a.binary.open('rb'),'sha256').hexdigest(),'machine':hex(pe.FILE_HEADER.Machine),'image_base':hex(pe.OPTIONAL_HEADER.ImageBase),'entry_rva':hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint),'imports':imports,'exports':exports}
a.output.write_text(json.dumps(meta,indent=2)+'\n');print(a.binary.name,'machine',meta['machine'],'imports',sum(len(x['symbols']) for x in imports),'exports',len(exports))
