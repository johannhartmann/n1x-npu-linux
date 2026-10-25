#!/usr/bin/env python3
"""Execute the original interface-1 loadable compatibility branch in isolation."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH, CTX, OUT, STACK, STOP
from inspect_loadable import inspect


def check(pe, selector, required, provided, callback_status=0):
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for page in (0x20000,0xa5000):
        u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
    for address in (CTX,OUT,STACK,STOP):u.mem_map(address,0x10000)
    u.mem_write(CTX,struct.pack('<Q',CTX+0x1000))
    u.mem_write(CTX+0x11d0,struct.pack('<Q',STOP+0x20))
    u.mem_write(CTX+0x98,struct.pack('<I',selector))
    u.mem_write(OUT+0x80,bytes(required))
    u.mem_write(BASE+0xa5340,struct.pack('<Q',STOP+0x40))
    outcome=[]
    calls=[]
    def hook(_,address,size,user):
        if address in (BASE+0x20ee0,BASE+0x20fc4):
            outcome.append('continue' if address==BASE+0x20ee0 else 'reject')
            u.emu_stop()
        elif address==STOP+0x20:
            assert u.reg_read(r.UC_ARM64_REG_X0)==CTX
            assert u.reg_read(r.UC_ARM64_REG_X1)==OUT+4
            assert bytes(u.mem_read(OUT+4,3))==bytes(3)
            u.mem_write(OUT+4,bytes(provided));calls.append(True)
            u.reg_write(r.UC_ARM64_REG_X0,callback_status)
            u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
        elif address==STOP+0x40:
            u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    u.reg_write(r.UC_ARM64_REG_X22,CTX);u.reg_write(r.UC_ARM64_REG_X26,OUT)
    u.reg_write(r.UC_ARM64_REG_SP,STACK+0xf000)
    u.emu_start(BASE+0x20e70,STOP,count=1000)
    assert calls==[True] and len(outcome)==1
    expected_error=callback_status or (0 if provided[0]==required[0] and provided[1]>=required[1] else 0x430005)
    assert u.reg_read(r.UC_ARM64_REG_W19)==expected_error
    assert outcome==['continue' if selector==3 or not expected_error else 'reject']
    return dict(selector=selector,required=required,provided=provided,callback_status=callback_status,
                comparison_status=expected_error,outcome=outcome[0])


def main():
    path=ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==RUNTIME_HASH
    pe=pefile.PE(str(path),fast_load=True)
    cases=[]
    for selector in (0,1,2,3,0x7fffffff):
        for required,provided,status in [([2,1,0],[3,0,0],0),([3,0,0],[3,0,0],0),
                                          ([3,1,0],[3,2,0],0),([3,2,0],[3,1,0],0),
                                          ([3,0,255],[3,0,0],0),([3,0,0],[3,0,0],0x65)]:
            cases.append(check(pe,selector,required,provided,status))
    inventory=[]
    for name in ('nvapi64.dll','nvcudaa64.dll','nvcudla.dll','nvdla_runtime.dll','nvlddmkm.sys'):
        data=(path.parent/name).read_bytes()
        hits=[]
        offset=0
        while (offset:=data.find(b'NVDA',offset))>=0:
            if offset>=4:
                try:
                    metadata=inspect(data[offset-4:])
                    hits.append(dict(file_offset=offset-4,version=metadata['version_bytes'],
                                     blobs=len(metadata['blobs']),tasks=len(metadata['tasks'])))
                except (ValueError,AssertionError,UnicodeError,IndexError,OverflowError):pass
            offset+=4
        inventory.append(dict(file=name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
                              projected_nvda_candidates=hits))
    print(json.dumps(dict(passed=True,hardware_access=False,cases=cases,inventory=inventory,
                         limitations=['Only branch 0x20e70 through its continue/reject destination executes',
                                      'Version callback and CFG are modeled; metadata is synthetic',
                                      'Continue still enters later compatibility checks, not completed loading or execution',
                                      'No model is modified and no physical model compatibility is established']),indent=2))


if __name__=='__main__':main()
