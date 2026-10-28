#!/usr/bin/env python3
import argparse,json,re,struct,hashlib
from pathlib import Path
parser=argparse.ArgumentParser(description='Inspect DLA resources from a supplied DSDT disassembly and live ACPI metadata.');parser.add_argument('--dsdt',type=Path,required=True);args=parser.parse_args();dsdt=args.dsdt;s=dsdt.read_text();block=s[s.index('        Device (DLA0)'):s.index('        Device (SVP0)')]
a=block.index('Name (_CRS');z=block.index('Method (_STA');raw=bytes(int(v,16) for v in re.findall(r'0x([0-9A-Fa-f]{2})(?=[, ])',re.sub(r'/\*.*?\*/|//[^\n]*','',block[a:z])))
# Remove the Buffer length, which is before the opening brace.
text=re.sub(r'/\*.*?\*/|//[^\n]*','',block[a:z]);text=text[text.index('{')+1:text.rindex('}')];raw=bytes(int(v,16) for v in re.findall(r'0x([0-9A-Fa-f]{2})',text))
resources=[];i=0
while i<len(raw):
 tag=raw[i]
 if tag==0x79:break
 if not tag&0x80:raise ValueError('unexpected small descriptor')
 n=struct.unpack_from('<H',raw,i+1)[0];p=raw[i+3:i+3+n]
 if tag==0x86:
  addr,length=struct.unpack_from('<II',p,1);resources.append({'kind':'Memory32Fixed','base':hex(addr),'length':hex(length),'read_write_flag':p[0]})
 elif tag==0x89:
  resources.append({'kind':'ExtendedInterrupt','flags':hex(p[0]),'gsi':list(struct.unpack_from('<'+'I'*p[1],p,2))})
 else:raise ValueError(hex(tag))
 i+=n+3
p=Path('/sys/bus/acpi/devices/NVDA200A:00');platform=Path('/sys/bus/platform/devices/NVDA200A:00')
out={'dsdt_source':str(dsdt),'dsdt_sha256':hashlib.sha256(dsdt.read_bytes()).hexdigest(),'hardware_id':'NVDA200A','compatible_id':'NVDA2042','hardware_revision':'0x34','path':(p/'path').read_text().strip(),'status':(p/'status').read_text().strip(),'modalias':(p/'modalias').read_text().strip(),'driver_bound':(platform/'driver').exists(),'iommu_group':str((platform/'iommu_group').resolve()),'resources':resources}
print(json.dumps(out,indent=2))
