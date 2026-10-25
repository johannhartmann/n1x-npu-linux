#!/usr/bin/env python3
"""Execute the original interface-1 loadable compatibility branch in isolation."""
import argparse
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_runtime_selector import ROOT, BASE, RUNTIME_HASH, CTX, OUT, STACK, STOP
from inspect_loadable import Buffer,inspect
from verify_relu_loadable import LOADABLE_HASH


def check(pe, selector, required, provided, callback_status=0, original_provider=False, cached=False):
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    for page in (0x20000,0xa5000):
        u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
    for address in (CTX,OUT,STACK,STOP):u.mem_map(address,0x10000)
    u.mem_write(CTX,struct.pack('<Q',CTX+0x1000))
    u.mem_write(CTX+0x11d0,struct.pack('<Q',STOP+0x20))
    u.mem_write(CTX+0x98,struct.pack('<I',selector))
    u.mem_write(OUT+0x80,bytes(required))
    u.mem_write(BASE+0xa5340,struct.pack('<Q',STOP+0x40))
    if original_provider:
        assert callback_status==0 and provided==[3,0,0]
        for page in (0x25000,0x75000,0x653000):
            u.mem_map(BASE+page,4096);u.mem_write(BASE+page,pe.get_data(page,4096))
        assert struct.unpack('<Q',pe.get_data(0x653d88+0x1d0,8))[0]==BASE+0x25410
        assert struct.unpack('<Q',pe.get_data(0x653d88+0x200,8))[0]==BASE+0x25460
        assert struct.unpack('<Q',pe.get_data(0x653d88+8,8))[0]==BASE+0x25630
        u.mem_write(CTX,struct.pack('<Q',BASE+0x653d88))
        u.mem_write(CTX+0x80,struct.pack('<Q',OUT+0x1000))
        u.mem_write(CTX+0x68,struct.pack('<Q',OUT+0x2000))
        if cached:u.mem_write(OUT+0x1000,bytes(provided))
    outcome=[]
    calls=[]
    provider_entries=[]
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
        elif original_provider and address in (BASE+0x25410,BASE+0x25460,BASE+0x25630,BASE+0x75810):
            provider_entries.append(hex(address-BASE))
            if address==BASE+0x25410:
                assert u.reg_read(r.UC_ARM64_REG_X0)==CTX and u.reg_read(r.UC_ARM64_REG_X1)==OUT+4
                calls.append(True)
        elif address==STOP+0x40:
            u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    u.reg_write(r.UC_ARM64_REG_X22,CTX);u.reg_write(r.UC_ARM64_REG_X26,OUT)
    u.reg_write(r.UC_ARM64_REG_SP,STACK+0xf000)
    u.emu_start(BASE+0x20e70,STOP,count=1000)
    assert calls==[True] and len(outcome)==1
    if original_provider:
        assert bytes(u.mem_read(OUT+4,3))==bytes(provided)
        assert bytes(u.mem_read(OUT+0x1000,3))==bytes(provided)
        assert provider_entries==['0x25410','0x25460','0x25630']+([] if cached else ['0x75810'])
    expected_error=callback_status or (0 if provided[0]==required[0] and provided[1]>=required[1] else 0x430005)
    assert u.reg_read(r.UC_ARM64_REG_W19)==expected_error
    assert outcome==['continue' if selector==3 or not expected_error else 'reject']
    return dict(selector=selector,required=required,provided=provided,callback_status=callback_status,
                comparison_status=expected_error,outcome=outcome[0],original_provider=original_provider,
                provider_entries=provider_entries)


def schema_prefix(pe,data,length=None):
    """Execute the original five validators and root-major check, then stop.

    Only mutex lock/unlock are modeled; no parser validator returns a shimmed
    success. Reaching the first materializer is not completed deserialization.
    """
    u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    u.mem_map(BASE,(pe.OPTIONAL_HEADER.SizeOfImage+4095)&~4095)
    u.mem_write(BASE,pe.get_memory_mapped_image())
    context,model,stack,stop=0x40000000,0x50000000,0x60000000,0x70000000
    for address,size in ((context,4096),(model,(len(data)+4095)&~4095),(stack,65536),(stop,4096)):
        u.mem_map(address,size)
    u.mem_write(model,data)
    validators=[];mutex_calls=[]
    entries=(0x80518,0x80f40,0x80a00,0x80130,0x7ff08)
    accepted=[]
    def hook(_,address,size,user):
        if address in (BASE+0x6b290,BASE+0x6b2d8):
            assert u.reg_read(r.UC_ARM64_REG_X0)==context+8
            mutex_calls.append(hex(address-BASE))
            u.reg_write(r.UC_ARM64_REG_X0,0)
            u.reg_write(r.UC_ARM64_REG_PC,u.reg_read(r.UC_ARM64_REG_LR))
        elif address==BASE+0x889cc:
            accepted.append(True);u.emu_stop()
        else:validators.append(hex(address-BASE))
    for address in (0x6b290,0x6b2d8,0x889cc,*entries):
        u.hook_add(UC_HOOK_CODE,hook,begin=BASE+address,end=BASE+address)
    for reg,value in ((r.UC_ARM64_REG_X0,context),(r.UC_ARM64_REG_X1,model),
                      (r.UC_ARM64_REG_X2,len(data) if length is None else length),
                      (r.UC_ARM64_REG_SP,stack+65536),(r.UC_ARM64_REG_LR,stop)):
        u.reg_write(reg,value)
    u.emu_start(BASE+0x888a0,stop,count=1000000)
    pc=u.reg_read(r.UC_ARM64_REG_PC)
    assert pc==(BASE+0x889cc if accepted else stop)
    assert mutex_calls==(['0x6b290'] if accepted else ['0x6b290','0x6b2d8'])
    return dict(accepted_by_prefix=bool(accepted),validators=validators,
                rejection_status=None if accepted else u.reg_read(r.UC_ARM64_REG_W0),
                endpoint=hex(pc-BASE) if accepted else 'return',mutex_calls=mutex_calls)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relu',action='store_true',help='Test original schema/root-version validation on the compiled ReLU fixture')
    args=parser.parse_args()
    path=ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==RUNTIME_HASH
    pe=pefile.PE(str(path),fast_load=True)
    if args.relu:
        data=(ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
        assert hashlib.sha256(data).hexdigest()==LOADABLE_HASH
        view=Buffer(data);version=view.field(view.u('<I',0),0)
        assert data[version:version+3]==bytes((0,9,0))
        major=bytearray(data);major[version]=1
        truncated=bytearray(data);struct.pack_into('<I',truncated,0,len(data)+4096)
        rows=[]
        for name,candidate,length,accepted,status in (
            ('original_relu',data,None,True,None),
            ('unsupported_root_major',bytes(major),None,False,0x430003),
            ('length_below_root_word',data,3,False,11),
            ('root_outside_buffer',bytes(truncated),None,False,11),
            ('truncated_payload',data,len(data)-256,False,11),
        ):
            result=schema_prefix(pe,candidate,length)
            assert result['accepted_by_prefix']==accepted and result['rejection_status']==status,(name,result)
            if accepted:assert result['validators']==['0x80518','0x80f40','0x80a00','0x80130','0x7ff08']
            rows.append(dict(case=name,**result))
        network=next(t for t in view.tables(view.u('<I',0),5) if view.string(t,0)=='task-0-nw')
        field=view.field(network,4);required=list(data[field:field+3]);assert required==[3,0,0]
        gates=[check(pe,3,required,[3,0,0],original_provider=True,cached=cached) for cached in (False,True)]
        assert all(g['outcome']=='continue' and g['comparison_status']==0 for g in gates)
        print(json.dumps(dict(passed=True,hardware_access=False,loadable_sha256=LOADABLE_HASH,
            original_deserializer_rva='0x888a0',cases=rows,original_content_version_gates=gates,
            limitations=['Only the prefix through the first materializer boundary executes',
                         'Mutex lock/unlock use a single-thread model; context constructor is not executed',
                         'Version gate uses original provider chain with a synthetic selected-device/cache context and modeled CFG',
                         'Provider 0x75810 returns constant 0x30000; it does not query hardware',
                         'No complete deserialization, runtime initialization or device compatibility is established',
                         'Negative fixtures are changed only in emulator RAM; the compiled artifact is unchanged']),indent=2))
        return
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
