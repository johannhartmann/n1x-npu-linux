#!/usr/bin/env python3
"""Decode original Windows DLA HAL assignments in RAM, for explicit selectors.
Selector-to-physical-device mapping is a separate, unproven step.
"""
from pathlib import Path
import hashlib,json,struct
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM
from unicorn.arm64_const import UC_ARM64_REG_X0,UC_ARM64_REG_LR
from extract_firmware import KMD_SHA256
root=Path(__file__).resolve().parents[1];path=root/'sources/nvidia/Display.Driver/nvlddmkm.sys'
assert hashlib.file_digest(path.open('rb'),'sha256').hexdigest()==KMD_SHA256
pe=pefile.PE(str(path),fast_load=True);va=0x141951000;code=pe.get_data(va-pe.OPTIONAL_HEADER.ImageBase,0xd000);rows=[]
for selector in (0,1,2):
 u=Uc(UC_ARCH_ARM64,UC_MODE_ARM);u.mem_map(va,0xd000);u.mem_write(va,code);u.mem_map(0x20000000,0x2000);u.mem_map(0x20300000,0x1000)
 u.mem_write(0x20000496,struct.pack('<H',selector));u.reg_write(UC_ARM64_REG_X0,0x20000000);u.reg_write(UC_ARM64_REG_LR,0x20300000)
 u.emu_start(0x141951238,0x20300000,count=50000)
 out={hex(o):hex(struct.unpack('<Q',u.mem_read(0x20000000+o,8))[0]) for o in range(0x78,0x460,8)}
 if selector==2:
  assert out['0x458']=='0x140d4a9a0';assert out['0x3d0']=='0x140d4a9c0'
 rows.append({'selector':selector,'mapping_to_live_device':'not yet established','callbacks':out})
print(json.dumps(rows,indent=2))
