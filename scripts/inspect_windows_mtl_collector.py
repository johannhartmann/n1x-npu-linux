#!/usr/bin/env python3
"""Replay original Windows MTL input collection using captured ReLU buffers.

Original collector, dispatch, SDP constructor and early builder execute with
explicit allocator, memcpy, single-thread synchronization and CRT shims.
No host OS call, complete graph translation or device operation executes.
"""
import hashlib,json,struct
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT


def main():
    path=ROOT/'sources/tensorrt-11.4/nvdla_compiler.dll'
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest=='0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d'
    captured=ROOT/'results/native-compile-t254'
    manifest=json.loads((captured/'mtl-capture.json').read_text())
    entries={e['kind']:e for e in manifest['entries']}
    expected={0:44,1:4376,2:236,3:34816,4:1368}
    buffers={}
    for kind,size in expected.items():
        entry=entries[kind];data=(captured/entry['file']).read_bytes()
        assert entry['bytes']==len(data)==size
        assert hashlib.sha256(data).hexdigest()==entry['sha256']
        buffers[kind]=data
    pe=pefile.PE(str(path));base=pe.OPTIONAL_HEADER.ImageBase
    image=pe.get_memory_mapped_image();u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    u.mem_map(base,(len(image)+4095)&~4095);u.mem_write(base,image)
    heap,stack,stop,teb=0x40000000,0x41000000,0x42000000,0x43000000
    u.mem_map(heap,0x100000);u.mem_map(stack,0x40000)
    u.mem_write(stack,b'\xa5'*0x40000)
    u.mem_map(stop,4096);u.mem_map(teb,4096)
    u.mem_write(teb+0x10,struct.pack('<Q',stack))
    tls=pe.DIRECTORY_ENTRY_TLS.struct
    tls_data=pe.get_data(tls.StartAddressOfRawData-base,tls.EndAddressOfRawData-tls.StartAddressOfRawData)
    u.mem_write(heap+0x40000,tls_data+b'\0'*tls.SizeOfZeroFill)
    u.mem_write(teb+0x58,struct.pack('<Q',teb+0x100))
    u.mem_write(teb+0x100,struct.pack('<Q',heap+0x40000))
    u.mem_write(tls.AddressOfIndex,struct.pack('<I',0))
    pointers={};cursor=heap+0x1000
    for kind,data in buffers.items():
        pointers[kind]=cursor;u.mem_write(cursor,data);cursor+=(len(data)+4095)&~4095
    common,vector,output=heap+0x100,heap+0x200,heap+0x20000
    # Layout copied by Windows wrapper 0x4cab40; host pointers are relocated.
    descriptor=bytearray(64)
    struct.pack_into('<QI',descriptor,0,pointers[0],1)
    struct.pack_into('<3Q',descriptor,16,pointers[2],pointers[3],pointers[1])
    struct.pack_into('<Q',descriptor,48,pointers[4])
    u.mem_write(common,bytes(descriptor))
    u.mem_write(vector,struct.pack('<3Q',output,output,output+0x9f60))
    for i,rva in enumerate((0x1052088,0x1052090,0x1052098)):
        u.mem_write(base+rva,struct.pack('<Q',stop+0x100+i*4))
    u.mem_write(base+0x10520c8,struct.pack('<Q',stop+0x114))
    copies=[]
    allocation_cursor=heap+0x60000
    shims={}
    def observed(name):shims[name]=shims.get(name,0)+1
    def hook(uc,address,size,user):
        nonlocal allocation_cursor
        if address==stop+0x114:
            observed('GetCurrentThreadId=1')
            uc.reg_write(r.UC_ARM64_REG_X0,1)
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR));return
        if address in (stop+0x100,stop+0x104,stop+0x108):
            observed('single-thread SRW synchronization')
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR));return
        if address==base+0xf02e08:
            observed('atexit registration; callbacks not executed')
            uc.reg_write(r.UC_ARM64_REG_X0,0)
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR));return
        if address==base+0xf0c6b8:
            observed('getenv URID_RANDOM unset')
            assert bytes(uc.mem_read(uc.reg_read(r.UC_ARM64_REG_X0),12))==b'URID_RANDOM\0'
            uc.reg_write(r.UC_ARM64_REG_X0,0)
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR));return
        if address in (base+0xf028b0,base+0xf02908):
            observed('operator new' if address==base+0xf028b0 else 'operator delete; retained until emulator exit')
            if address==base+0xf028b0:
                n=uc.reg_read(r.UC_ARM64_REG_X0);assert 0<n<0x10000,n
                assert allocation_cursor+n<heap+0x100000
                ptr=allocation_cursor;allocation_cursor+=(n+15)&~15
                uc.mem_write(ptr,b'\xa5'*n);uc.reg_write(r.UC_ARM64_REG_X0,ptr)
            uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR));return
        if address!=base+0x1200:return
        dst,src,n=(uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2))
        assert 0<=n<=0x9f60
        if n:uc.mem_write(dst,bytes(uc.mem_read(src,n)))
        copies.append(n)
        uc.reg_write(r.UC_ARM64_REG_X0,dst)
        uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    for reg,value in ((r.UC_ARM64_REG_X0,0),(r.UC_ARM64_REG_X1,common),(r.UC_ARM64_REG_X2,vector),
                      (r.UC_ARM64_REG_X18,teb),(r.UC_ARM64_REG_SP,stack+0x40000),(r.UC_ARM64_REG_LR,stop)):
        u.reg_write(reg,value)
    u.emu_start(base+0x756630,stop,count=20000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==stop
    assert struct.unpack('<3Q',u.mem_read(vector,24))==(output,output+0x9f60,output+0x9f60)
    record=bytes(u.mem_read(output,0x9f60))
    assert record[:236]==buffers[2]
    assert record[236:240]==b'\xa5'*4
    assert record[0xf0:0x88f0]==buffers[3]
    assert record[0x88f0:0x9a08]==buffers[1]
    assert record[0x9a08:]==b'\xa5'*(0x9f60-0x9a08)
    assert copies==[0x8800,0x1118,0x9f60]
    # Execute the original IR dispatch, stopping before the selected builder.
    ir_object,graph_object=heap+0x400,heap+0x800
    for reg,value in ((r.UC_ARM64_REG_X19,common),(r.UC_ARM64_REG_X20,graph_object),
                      (r.UC_ARM64_REG_X21,ir_object),(r.UC_ARM64_REG_X26,heap+0x600)):
        u.reg_write(reg,value)
    u.emu_start(base+0x70e984,base+0x6f9928,count=100)
    assert u.reg_read(r.UC_ARM64_REG_PC)==base+0x6f9928
    arguments=[u.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2,
                                         r.UC_ARM64_REG_X3,r.UC_ARM64_REG_X4,r.UC_ARM64_REG_X5,r.UC_ARM64_REG_X6)]
    assert arguments==[ir_object,pointers[2],pointers[1],pointers[3],pointers[0],pointers[4],graph_object]
    assert buffers[1][0x1004]==2
    assert pe.get_data(0x1166560,13)==b'MtlEngineSdp\0'
    def invoke(address,*args,until=stop):
        for reg,value in zip((r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2,r.UC_ARM64_REG_X3,
                              r.UC_ARM64_REG_X4,r.UC_ARM64_REG_X5,r.UC_ARM64_REG_X6),args):u.reg_write(reg,value)
        u.reg_write(r.UC_ARM64_REG_SP,stack+0x40000);u.reg_write(r.UC_ARM64_REG_LR,stop)
        u.emu_start(base+address,until,count=200000)
        assert u.reg_read(r.UC_ARM64_REG_PC)==until
        return u.reg_read(r.UC_ARM64_REG_X0)
    # Call actual registry constructors, not the whole DLL/CRT initializer list.
    invoke(0xb7f40,base+0x18cb7d0)
    assert invoke(0x6e34e8,heap+0x50000)==heap+0x50000
    assert struct.unpack('<Q',u.mem_read(heap+0x50000,8))[0]==base+0x11664d8
    invoke(0x6152e8,base+0x18ccbb0)
    invoke(0xb7f40,base+0x18ccc30)
    invoke(0x614da8,ir_object+8)
    invoke(0x614f68,ir_object+0x48)
    invoke(0x614f68,ir_object+0x88)
    invoke(0x616660,graph_object)
    u.mem_write(ir_object,struct.pack('<Q',base+0x114e968))
    invoke(0x6f9928,*arguments,until=base+0x6f9a38)
    builder_object=u.reg_read(r.UC_ARM64_REG_X19)
    assert bytes(u.mem_read(builder_object+0x1b8,4376))==buffers[1]
    assert bytes(u.mem_read(builder_object+0x12f0,156))==buffers[2][:156]
    assert struct.unpack('<Q',u.mem_read(builder_object,8))[0]==base+0x11664d8

    print(json.dumps(dict(passed=True,hardware_access=False,windows_sha256=digest,
        collector_rva='0x756630',operation_count=1,record_bytes=len(record),
        dispatch=dict(surface_type_offset='0x1004',surface_type=2,builder_rva='0x6f9928',
                      constructor_rva='0x6e34e8',constructor_label='MtlEngineSdp',
                      original_dispatch_executed=True,original_constructor_completed=True,
                      builder_stopped_at_rva="0x6f9a38",builder_completed=False,
                      verified_object_regions=[dict(offset="0x1b8",bytes=4376,source_kind=1),
                                               dict(offset="0x12f0",bytes=156,source_kind=2)],
                      verified_argument_source_kinds=[None,2,1,3,0,4,None]),
        original_stack_helpers_executed=True,memcpy_shim_sizes=copies,runtime_shims=shims,
        verified_regions=[dict(offset=0,bytes=236,source_kind=2),dict(offset=240,bytes=34816,source_kind=3),
                          dict(offset=35056,bytes=4376,source_kind=1)],
        untouched_regions=[dict(offset=236,bytes=4),dict(offset=39432,bytes=1368)],
        limitations=['Collector uses a preallocated record; builder graph vectors execute original growth with modeled allocation',
                     'Copied buffers match the captured native input, not translated firmware output',
                     'No backend configuration, instruction lowering, loadable or inference is established']),indent=2))


if __name__=='__main__':main()
