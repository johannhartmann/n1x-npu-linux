#!/usr/bin/env python3
"""Replay original Windows backend-3 translation on captured native ReLU inputs.

Runs in Unicorn with explicit Windows/CRT models and no host API dispatch.
Exports checked backend buffers, not a complete loadable or physical inference.
"""
import argparse,hashlib,json,struct
from collections import Counter
import pefile
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
from unicorn import arm64_const as r
from inspect_dla_compiler import ROOT,NATIVE_HASH


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--translation-input',action='store_true',
                        help='Use the captured second frontend handoff after real pretranslation output replay')
    args=parser.parse_args()
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
    u.ctl_set_cpu_model(r.UC_CPU_ARM64_MAX)
    u.mem_map(base,(len(image)+4095)&~4095);u.mem_write(base,image)
    heap,stack,stop,teb=0x40000000,0x50000000,0x51000000,0x52000000
    u.mem_map(heap,0x8000000);u.mem_map(stack,0x40000)
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
    import_names = (
        'ReleaseSRWLockExclusive', 'AcquireSRWLockExclusive', 'WakeAllConditionVariable',
        'GetCurrentThreadId', 'LoadLibraryExW', 'GetProcAddress', 'FreeLibrary',
        'EnterCriticalSection', 'LeaveCriticalSection', 'VirtualProtect',
        'HeapAlloc', 'HeapFree', 'EncodePointer', 'GetLastError', 'SetLastError',
        'FlsAlloc', 'FlsGetValue', 'FlsSetValue', 'FlsFree', 'GetProcessHeap',
        'MultiByteToWideChar', 'GetFileAttributesExW', 'WideCharToMultiByte',
        'CreateDirectoryW', 'QueryPerformanceFrequency', 'QueryPerformanceCounter',
        'InitializeCriticalSectionEx', 'DeleteCriticalSection',
        'GetStartupInfoW', 'GetStdHandle', 'CreateFileW',
    )
    import_stubs={name:stop+0x100+4*i for i,name in enumerate(import_names)}
    import_stubs['AreFileApisANSI']=stop+0x200
    found=set()
    for dll in pe.DIRECTORY_ENTRY_IMPORT:
        for entry in dll.imports:
            name=entry.name.decode() if entry.name else ''
            if name in import_stubs:
                u.mem_write(entry.address,struct.pack('<Q',import_stubs[name]));found.add(name)
    assert found==set(import_stubs)
    stub_names={address:name for name,address in import_stubs.items()}
    optional_functions={
        'api-ms-win-core-fibers-l1-1-2': {'FlsGetValue2'},
        'api-ms-win-core-string-l1-1-0': {'CompareStringEx'},
        'api-ms-win-core-localization-l1-2-1': {
            'EnumSystemLocalesEx','GetLocaleInfoEx','GetUserDefaultLocaleName',
            'IsValidLocaleName','LCMapStringEx','LocaleNameToLCID'},
        'api-ms-win-core-datetime-l1-1-1': {'GetDateFormatEx','GetTimeFormatEx'},
        'api-ms-win-core-localization-obsolete-l1-2-0': {'LCIDToLocaleName','LocaleNameToLCID'},
        'kernel32': {'AreFileApisANSI'},
    }
    modules={};fls={};fls_callbacks={};last_error=0
    allocation_cursor=heap+0x60000
    allocations={};retired=set();copies=[];shims=Counter();queries=[]
    graph_edges=[]
    initializer_calls=[];error_flags=[];file_events=[];path_conversions=set()
    statistics_builder=[]
    modeled_ticks=0
    virtual_directories=set()
    process_heap=0x12340000  # An opaque handle, never dereferenced.
    def allocate(n,zero=False):
        nonlocal allocation_cursor
        assert 0<n<=0x1000000 and allocation_cursor+n<heap+0x8000000,(n,hex(allocation_cursor))
        ptr=allocation_cursor;allocation_cursor+=(n+15)&~15
        u.mem_write(ptr,(b'\0' if zero else b'\xa5')*n)
        allocations[ptr]=n
        return ptr
    def retire(ptr):
        if ptr:
            assert ptr in allocations and ptr not in retired,hex(ptr)
            retired.add(ptr)  # No reuse; emulator exit releases all storage.
    def cstring(ptr,wide=False):
        width=2 if wide else 1;value=bytearray()
        for i in range(256):
            char=bytes(u.mem_read(ptr+i*width,width))
            if char==b'\0'*width:return value.decode('utf-16-le' if wide else 'ascii')
            value.extend(char)
        raise AssertionError('Unterminated runtime API name')
    def hook(uc,address,size,user):
        nonlocal last_error,modeled_ticks
        if address==base+0xc76488:
            statistics_builder.append(dict(context=uc.reg_read(r.UC_ARM64_REG_X0)))
        if address==base+0xc76734:
            assert len(statistics_builder)==1 and uc.reg_read(r.UC_ARM64_REG_X0)==0
            context=statistics_builder[-1]['context']
            n=struct.unpack('<I',uc.mem_read(context+0x34,4))[0]
            pointer=struct.unpack('<Q',uc.mem_read(context+0x38,8))[0]
            header=list(struct.unpack('<7I',uc.mem_read(pointer,28)))
            assert n==28736 and header==[0x300,2,4,6144,28,1024,24640]
            statistics_builder[-1].update(status=0,bytes=n,header_words=header)
        if address==base+0xf19384:initializer_calls.append(uc.reg_read(r.UC_ARM64_REG_X15)-base)
        if address==base+0xc6ddd8 and uc.reg_read(r.UC_ARM64_REG_X0):
            error_flags.append(uc.reg_read(r.UC_ARM64_REG_X0))

        if address==base+0x61e1b0:
            graph_edges.append(struct.unpack('<2I',uc.mem_read(uc.reg_read(r.UC_ARM64_REG_X1),8)))
        if address==base+0x61e298:
            assert uc.reg_read(r.UC_ARM64_REG_W23)==0,'Original edge insertion failed'
        name=stub_names.get(address)
        if name:
            shims[name]+=1
            x0,x1,x2,x3=(uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2,r.UC_ARM64_REG_X3))
            result=x0
            if name in ('ReleaseSRWLockExclusive','AcquireSRWLockExclusive',
                        'WakeAllConditionVariable','EnterCriticalSection','LeaveCriticalSection'):
                pass  # Explicit single-thread model; not a Windows lock implementation.
            elif name=='CreateFileW':
                filename=cstring(x0,wide=True);disposition=uc.reg_read(r.UC_ARM64_REG_W4)
                file_events.append(dict(api=name,path=filename,access=hex(x1),disposition=disposition))
                assert filename=='.//compile_opt.json' and x1==0x80000000 and disposition==3
                result=0xffffffffffffffff;last_error=2
            elif name=='GetStartupInfoW':
                uc.mem_write(x0,struct.pack('<I',104)+b'\0'*100)
            elif name=='GetStdHandle':
                assert x0&0xffffffff in (0xfffffff6,0xfffffff5,0xfffffff4);result=0
            elif name=='InitializeCriticalSectionEx':
                assert x0 and x2 in (0,0x1000000);result=1
            elif name=='DeleteCriticalSection':pass
            elif name=='MultiByteToWideChar':
                dst,capacity=uc.reg_read(r.UC_ARM64_REG_X4),uc.reg_read(r.UC_ARM64_REG_W5)
                assert x0==0 and x1 in (8,9) and (0<x3<256 or x3==0xffffffff),(x0,x1,x3,cstring(x2))
                raw=(cstring(x2)+'\0').encode('ascii') if x3==0xffffffff else bytes(uc.mem_read(x2,x3))
                path_conversions.add(raw.decode('ascii').rstrip('\0'))
                encoded=raw.decode('ascii').encode('utf-16-le');result=len(encoded)//2
                if capacity:
                    assert dst and capacity>=result;uc.mem_write(dst,encoded)
            elif name=='WideCharToMultiByte':
                dst,capacity=uc.reg_read(r.UC_ARM64_REG_X4),uc.reg_read(r.UC_ARM64_REG_W5)
                default,used=uc.reg_read(r.UC_ARM64_REG_X6),uc.reg_read(r.UC_ARM64_REG_X7)
                assert x0==0 and x1==0x400 and default==0 and 0<x3<256
                raw=bytes(uc.mem_read(x2,x3*2));encoded=raw.decode('utf-16-le').encode('ascii')
                path_conversions.add(encoded.decode('ascii').rstrip('\0'))
                result=len(encoded)
                if used:uc.mem_write(used,struct.pack('<I',0))
                if capacity:
                    assert dst and capacity>=result;uc.mem_write(dst,encoded)
            elif name=='CreateDirectoryW':
                filename=cstring(x0,wide=True);file_events.append(dict(api=name,path=filename))
                assert filename in ('/graph','/graph/kernel_compile','/graph/linker') and x1==0
                assert filename=='/graph' or '/graph' in virtual_directories
                if filename in virtual_directories:result=0;last_error=183
                else:virtual_directories.add(filename);result=1
            elif name=='GetFileAttributesExW':
                filename=cstring(x0,wide=True);file_events.append(dict(api=name,path=filename))
                assert x1==0
                if filename=='./compile_opt.json':result=0;last_error=2
                else:
                    assert filename in virtual_directories
                    uc.mem_write(x2,struct.pack('<I',16)+b'\0'*32);result=1
            elif name in ('QueryPerformanceFrequency','QueryPerformanceCounter'):
                if name=='QueryPerformanceCounter':modeled_ticks+=1000
                uc.mem_write(x0,struct.pack('<Q',1000000 if name=='QueryPerformanceFrequency' else modeled_ticks));result=1
            elif name=='GetCurrentThreadId':result=1
            elif name=='GetProcessHeap':result=process_heap
            elif name=='LoadLibraryExW':
                module=cstring(x0,wide=True)
                assert module in optional_functions and x1==0
                if module not in modules:modules[module]=0x1234+len(modules)
                result=modules[module]
            elif name=='GetProcAddress':
                module=next(key for key,value in modules.items() if value==x0)
                function=cstring(x1);assert function in optional_functions[module],(module,function)
                queries.append(dict(module=module,function=function))
                result=import_stubs['AreFileApisANSI'] if function=='AreFileApisANSI' else 0
                if not result:last_error=127  # ERROR_PROC_NOT_FOUND: original fallback executes.
            elif name=='AreFileApisANSI':result=1
            elif name=='FreeLibrary':
                assert x0 in modules.values();result=1
            elif name=='VirtualProtect':
                assert base<=x0 and 0<x1 and x0+x1<=base+len(image)
                uc.mem_write(x3,struct.pack('<I',4));result=1  # PE mapping is already RWX.
            elif name=='HeapAlloc':
                assert x0==process_heap and x1 in (0,8)
                result=allocate(x2,zero=bool(x1&8))
            elif name=='HeapFree':
                assert x0==process_heap and x1==0
                retire(x2);result=1
            elif name=='EncodePointer':pass  # Identity encoding in this isolated model.
            elif name=='GetLastError':result=last_error
            elif name=='SetLastError':last_error=x0&0xffffffff
            elif name=='FlsAlloc':
                assert base<=x0<base+len(image)
                result=max(fls,default=0)+1;fls[result]=0;fls_callbacks[result]=x0-base
            elif name in ('FlsGetValue','FlsSetValue','FlsFree'):
                assert x0 in fls,('Uninitialized FLS slot',x0)
                if name=='FlsGetValue':result=fls[x0];last_error=0
                elif name=='FlsSetValue':fls[x0]=x1;result=1
                else:del fls[x0];result=1
            else:raise AssertionError(name)
            uc.reg_write(r.UC_ARM64_REG_X0,result)
        elif address==base+0xf02e08:
            shims['atexit registration; callbacks not executed']+=1
            uc.reg_write(r.UC_ARM64_REG_X0,0)
        elif address==base+0xf0c6b8:
            key=cstring(uc.reg_read(r.UC_ARM64_REG_X0))
            assert key in ('URID_RANDOM','RANDOMIZE_UNUSED_REGISTER','RANDOM_SEED','VALIDATION_SYNC_STEP'),key
            shims[f'getenv {key} unset']+=1
            uc.reg_write(r.UC_ARM64_REG_X0,0)
        elif address in (base+0xf028b0,base+0xf02908):
            shims['operator new' if address==base+0xf028b0 else 'operator delete; no reuse']+=1
            ptr=uc.reg_read(r.UC_ARM64_REG_X0)
            if address==base+0xf028b0:uc.reg_write(r.UC_ARM64_REG_X0,allocate(ptr))
            else:retire(ptr)
        elif address==base+0x1200:
            dst,src,n=(uc.reg_read(reg) for reg in (r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2))
            assert 0<=n<=0x1000000
            if n:uc.mem_write(dst,bytes(uc.mem_read(src,n)))
            copies.append(n);uc.reg_write(r.UC_ARM64_REG_X0,dst)
        else:return
        uc.reg_write(r.UC_ARM64_REG_PC,uc.reg_read(r.UC_ARM64_REG_LR))
    for address in set(stub_names)|{base+x for x in (0xc76488,0xc76734,0xf19384,0xc6ddd8,0x61e1b0,0x61e298,0xf02e08,0xf0c6b8,0xf028b0,0xf02908,0x1200)}:
        u.hook_add(UC_HOOK_CODE,hook,begin=address,end=address)
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
    def invoke(address,*args,until=stop,instruction_limit=2000000):
        for reg,value in zip((r.UC_ARM64_REG_X0,r.UC_ARM64_REG_X1,r.UC_ARM64_REG_X2,r.UC_ARM64_REG_X3,
                              r.UC_ARM64_REG_X4,r.UC_ARM64_REG_X5,r.UC_ARM64_REG_X6),args):u.reg_write(reg,value)
        u.reg_write(r.UC_ARM64_REG_SP,stack+0x40000);u.reg_write(r.UC_ARM64_REG_LR,stop)
        u.emu_start(base+address,until,count=instruction_limit)
        assert u.reg_read(r.UC_ARM64_REG_PC)==until
        return u.reg_read(r.UC_ARM64_REG_X0)
    # Required original CRT setup precedes the complete C++ initializer array.
    assert invoke(0xf267d0)==1
    assert invoke(0xf190e0)==1
    assert struct.unpack('<Q',u.mem_read(base+0x1a19138,8))[0]==base+0x18ac930
    assert invoke(0xf1dce0)==1
    assert invoke(0xf2fa98)==1
    assert invoke(0xf1f750)==1
    assert invoke(0xf07dc0)==0
    initializer_array=pe.get_data(0x10523b0,0x1055af0-0x10523b0)
    expected_initializers=[ptr-base for (ptr,) in struct.iter_unpack('<Q',initializer_array) if ptr]
    assert len(expected_initializers)==1767
    invoke(0xf19358,base+0x10523b0,base+0x1055af0,instruction_limit=100000000)
    assert initializer_calls==expected_initializers
    assert invoke(0x6e34e8,heap+0x50000)==heap+0x50000
    assert struct.unpack('<Q',u.mem_read(heap+0x50000,8))[0]==base+0x11664d8
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

    u.emu_start(base+0x6f9a38,stop,count=1000000)
    assert u.reg_read(r.UC_ARM64_REG_PC)==stop
    assert u.reg_read(r.UC_ARM64_REG_X0)==0

    def qword(ptr):return struct.unpack('<Q',u.mem_read(ptr,8))[0]
    def dword(ptr):return struct.unpack('<I',u.mem_read(ptr,4))[0]
    def node_name(ptr):
        n,capacity=struct.unpack('<2Q',u.mem_read(ptr+0x90,16))
        assert n<256
        return bytes(u.mem_read(qword(ptr+0x80) if capacity>=16 else ptr+0x80,n)).decode('ascii')
    def verify_graph(graph,edges):
        begin,end,capacity=struct.unpack('<3Q',u.mem_read(graph+0xe0,24))
        assert end-begin==24 and end<=capacity
        sdp,source,dest=struct.unpack('<3Q',u.mem_read(begin,24))
        assert qword(sdp)==base+0x11664d8
        assert qword(source)==qword(dest)==base+0x11c56c0
        assert bytes(u.mem_read(sdp+0x1b8,4376))==buffers[1]
        assert bytes(u.mem_read(sdp+0x12f0,156))==buffers[2][:156]
        for ptr,offset in ((source,0),(dest,0x2e8)):
            assert bytes(u.mem_read(ptr+0x1b8,248))==buffers[3][offset:offset+248]
        source_id,sdp_id,dest_id=[dword(ptr+8) for ptr in (source,sdp,dest)]
        assert edges==[(source_id,sdp_id),(sdp_id,dest_id)]
        assert struct.unpack('<4I',u.mem_read(sdp+0x18e8,16))==(source_id,0,0,dest_id)
        assert [node_name(ptr) for ptr in (source,sdp,dest)]==['0_0','layer-anon-0','1_0']
        for ptr in (source,sdp,dest):
            assert invoke(0x622bf0,graph,dword(ptr+8))==ptr  # Original graph lookup.
        assert qword(graph+0x18)==3 and qword(graph+0x58)==2
        return dict(nodes=[dict(id=dword(ptr+8),name=node_name(ptr),vtable_rva=hex(qword(ptr)-base))
                           for ptr in (source,sdp,dest)],edges=edges,
                    operand_bytes_verified=248,operand_source_offsets=[0,0x2e8])
    builder_graph=verify_graph(graph_object,list(graph_edges))
    # Re-enter through the complete original IR routine with fresh contexts.
    # This also exercises its original graph constructor and postprocessing.
    ir2,graph2=heap+0xa00,heap+0xc00
    invoke(0x614da8,ir2+8)
    invoke(0x614f68,ir2+0x48)
    invoke(0x614f68,ir2+0x88)
    u.mem_write(ir2,struct.pack('<Q',base+0x114e968))
    graph_edges.clear()
    assert invoke(0x70e910,ir2,graph2,common)==graph2
    ir_graph=verify_graph(graph2,list(graph_edges))
    mtl=heap+0xe00
    assert invoke(0x617a70,mtl)==mtl
    assert invoke(0x624630,mtl,common,instruction_limit=10000000)==0
    assert bytes(u.mem_read(common,64))==bytes(descriptor)
    pre_payloads={kind:bytes(u.mem_read(pointers[kind],len(data))) for kind,data in buffers.items()}
    pre_hashes={1:'4d0745544f4f7c0128991b927595e5a8c9472b7b36170b698152349f4aebdb9f',
                3:'d6df2465b95418819f9ae10480cd61089f7a8d6f9a9198e82c2a2a6016d1aae7'}
    pre_outputs=[]
    for kind,data in pre_payloads.items():
        expected_hash=pre_hashes.get(kind,hashlib.sha256(buffers[kind]).hexdigest())
        assert hashlib.sha256(data).hexdigest()==expected_hash
        pre_outputs.append(dict(kind=kind,bytes=len(data),sha256=expected_hash,
                                input_sha256=hashlib.sha256(buffers[kind]).hexdigest(),
                                changed_offsets=[i for i,(a,b) in enumerate(zip(buffers[kind],data)) if a!=b]))
    pre_graph=qword(mtl+0xc0)
    assert pre_graph in allocations and pre_graph not in retired
    assert qword(pre_graph+0x18)==1 and qword(pre_graph+0x58)==0
    begin,end,capacity=struct.unpack('<3Q',u.mem_read(pre_graph+0xe0,24))
    assert end-begin==8 and end<=capacity
    node=qword(begin)
    assert qword(node)==base+0x1150a90 and node_name(node)=='layer-anon-0'
    assert invoke(0x622bf0,pre_graph,dword(node+8))==node
    assert not error_flags
    pretranslation=dict(rva='0x624630',completed=True,return_value=0,
                        graph_nodes=1,graph_edges=0,node_name=node_name(node),
                        node_vtable_rva=hex(qword(node)-base),optional_config='absent')
    handoff=dict(stage='pretranslation descriptor reused',operation_count=1)
    if args.translation_input:
        cap=captured/'translation-input'
        capture=json.loads((cap/'manifest.json').read_text())
        assert capture['native_sha256']==NATIVE_HASH and capture['windows_sha256']==digest
        raw=(cap/'common.bin').read_bytes()
        assert len(raw)==96 and hashlib.sha256(raw).hexdigest()==capture['common_sha256']
        assert struct.unpack_from('<I',raw,48)[0]==struct.unpack_from('<I',raw,80)[0]==0
        assert raw[48:]==bytes(48)
        network=(cap/'network.bin').read_bytes()
        assert len(network)==44 and hashlib.sha256(network).hexdigest()==capture['network_sha256']=='8c52127eee7cbbe584ed4ea87c94632fa30c4623527560074f38f1f4b0a7d9d2'
        final_entries={e['kind']:e for e in capture['entries']}
        for kind in (1,2,3,4):
            data=(cap/f'kind-{kind}.bin').read_bytes()
            expected_data=bytearray(pre_payloads[kind])
            if kind==3:
                # Original frontend assigns the input/output address-list indices.
                struct.pack_into('<I',expected_data,232,1)
                struct.pack_into('<I',expected_data,976,2)
            assert data==bytes(expected_data)
            assert hashlib.sha256(data).hexdigest()==final_entries[kind]['sha256']
            u.mem_write(pointers[kind],data)
        translation_network=allocate(len(network))
        u.mem_write(translation_network,network)
        # The second wrapper handoff contains only this new descriptor.
        # Input records have been retained by the backend during pretranslation.
        descriptor=bytearray(64)
        struct.pack_into('<Q',descriptor,0,translation_network)
        u.mem_write(common,bytes(descriptor))
        handoff=dict(stage='captured frontend translation descriptor',operation_count=0,
                     network_sha256=capture['network_sha256'],network_hex=network.hex(),
                     source=str(cap),input_sha256={e['kind']:e['sha256'] for e in capture['entries']})
    retained_before={kind:bytes(u.mem_read(pointers[kind],len(data))) for kind,data in buffers.items()}
    assert invoke(0x624eb0,mtl,common,instruction_limit=100000000)==0
    if args.translation_input:
        assert bytes(u.mem_read(common,64))==bytes(descriptor)
        assert bytes(u.mem_read(translation_network,44))==network
        assert all(bytes(u.mem_read(pointers[kind],len(data)))==data for kind,data in retained_before.items())
        handoff['inputs_unchanged_after_translation']=True
    assert not error_flags
    # Translation success does not imply that optional metrics were produced.
    # The original getter dereferences the pointer stored at object +0x50.
    assert qword(mtl+0x50)==0
    metrics_destination=allocate(36)
    metrics_status=invoke(0x624480,mtl,metrics_destination)
    assert metrics_status==0x7fffffff
    assert bytes(u.mem_read(metrics_destination,36))==b'\xa5'*36
    metrics=dict(getter_rva='0x624480',status=metrics_status,available=False,
                 destination_unchanged=True)
    begin,end,capacity=struct.unpack('<3Q',u.mem_read(mtl+0x58,24))
    assert end-begin==(7 if args.translation_input else 4)*24 and end<=capacity
    expected_outputs={
        0:(0,'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
        3:(256,'d40c6354059292dc4b2c3c7faca391b59240d305ce45b35da00c902ca67f778c'),
        6:(28736,'da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3'),
        7:(3624,'c1af31f16b73278e61f8e6b9b8cf3aedb3140c3caf79afc45c8e4623aa44d76d'),
    }
    if args.translation_input:
        expected_outputs={
            0:(30464,'eb1090b0e47dc472be8aec0290f0422f61f85ab34d540a807e9ffdc82bee2cbf'),
            1:(320,'b42e7caa0ee7f927aa6ce37f2002f1c377493a90772417d690eb0bb9b6e2ebc9'),
            3:(256,'86f4866d1a6a859b697a8a97548080bcfdc3ef21f06e2f9cb90efc44b75ae781'),
            4:(2738944,'1213200f5d0ad268d324b62729dbcd520ff32f4cb94b78bf5afcf536cc1b11a4'),
            5:(28736,'da9182fb36e22c8262eed1d4baf3d0383d9f90826d99d692c1139f51ec56f7c3'),
            6:(0,'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
            7:(10034,'2b5334a01d3f1d39385f053c8b2baeebe78c7e9e4271b339b98a37b892e9781a'),
        }
    outputs=[];payloads={}
    for slot in range(begin,end,24):
        descriptor=bytes(u.mem_read(slot,24))
        ptr=struct.unpack_from('<Q',descriptor)[0];kind=descriptor[12]
        n=struct.unpack_from('<Q',descriptor,16)[0]
        assert kind in expected_outputs and kind not in payloads
        assert n==expected_outputs[kind][0]
        data=bytes(u.mem_read(ptr,n)) if n else b''
        sha=hashlib.sha256(data).hexdigest()
        assert sha==expected_outputs[kind][1],(kind,sha)
        u.mem_write(heap+0xf00,bytes([kind]));u.mem_write(heap+0xf10,b'\xa5'*16)
        assert invoke(0x624420,mtl,heap+0xf00,heap+0xf10)==0
        assert qword(heap+0xf10)==n and bytes(u.mem_read(heap+0xf18,8))==b'\xa5'*8
        destination=allocate(n+16)
        copy_status=invoke(0x6243a0,mtl,heap+0xf00,destination)
        assert copy_status==(0 if n else 0x7fffffff)
        assert (bytes(u.mem_read(destination,n)) if n else b'')==data
        assert bytes(u.mem_read(destination+n,16))==b'\xa5'*16
        payloads[kind]=data
        outputs.append(dict(kind=kind,bytes=n,sha256=sha,file=f'kind-{kind}.bin',
                            original_size_and_copy_verified=True,copy_status=copy_status))
    metadata=json.loads(payloads[7])
    assert len(statistics_builder)==1 and statistics_builder[0]['status']==0
    assert metadata['schema_version']==1 and len(metadata['gp_layers'])==1
    assert len(metadata['layers'])==4
    es_lists=metadata['gp_layers'][0]['es_lists']
    assert metadata['gp_layers'][0]['id']==0 and len(es_lists)==4
    for index,entry in enumerate(es_lists):
        assert entry['es_type']==f'NVDLA_PAUSE_ES_CPIPE{index}' and entry['count']==2
        assert entry['blob_offset']==24832+index*384<len(payloads[5 if args.translation_input else 6])
        assert metadata['layers'][index]['name']==f'layer-anon-0_cpipe_tile_{index}_ts_hwl_cpipe_{index}'
        if args.translation_input:
            for side,address_index in (('input_tensors',1),('output_tensors',2)):
                tensors=metadata['layers'][index][side];assert len(tensors)==1
                tensor=tensors[0];storage=tensor['storage']
                assert (tensor['n'],tensor['c'],tensor['h'],tensor['w'])==(1,32,2 if index<3 else 1,7)
                assert tensor['dtype']=='DATA_TYPE_FP16'
                assert storage['address_list_index']==address_index
                assert storage['src_offset_bytes']==448*index
                assert (storage['batch_stride_bytes'],storage['line_stride_bytes'],storage['surface_stride_bytes'])==(3136,224,1568)
    postdir=ROOT/'results/windows-pretranslation-output';postdir.mkdir(exist_ok=True)
    for kind,data in pre_payloads.items():(postdir/f'kind-{kind}.bin').write_bytes(data)
    (postdir/'manifest.json').write_text(json.dumps(dict(passed=True,hardware_access=False,
        windows_sha256=digest,return_value=0,descriptor_unchanged=True,outputs=pre_outputs),indent=2)+'\n')
    # Write artifacts only after all checks. These are research intermediates.
    outdir=ROOT/('results/windows-final-backend-output' if args.translation_input else 'results/windows-backend-output')
    outdir.mkdir(exist_ok=True)
    for entry in outputs:(outdir/entry['file']).write_bytes(payloads[entry['kind']])
    output_manifest=dict(passed=True,hardware_access=False,complete_loadable=False,
                         windows_sha256=digest,source_capture='native-compile-t254',
                         input_sha256={kind:hashlib.sha256(data).hexdigest() for kind,data in buffers.items()},
                         pretranslation_return=0,translation_return=0,metrics=metrics,handoff=handoff,outputs=outputs)
    (outdir/'manifest.json').write_text(json.dumps(output_manifest,indent=2)+'\n')
    print(json.dumps(dict(passed=True,hardware_access=False,windows_sha256=digest,
        collector_rva='0x756630',operation_count=1,record_bytes=len(record),
        pretranslation=pretranslation,
        statistics_builder=dict(rva='0xc76488',status=0,bytes=statistics_builder[0]['bytes'],
                                header_words=statistics_builder[0]['header_words']),
        translation=dict(rva='0x624eb0',completed=True,return_value=0,outputs=outputs,
                         metrics=metrics,handoff=handoff,
                         output_manifest=str(outdir/'manifest.json'),complete_loadable=False),
        cpp_initializers=dict(runner_rva='0xf19358',array_begin_rva='0x10523b0',
                              array_end_rva='0x1055af0',count=len(initializer_calls),order_verified=True),
        allocator_bytes_used=allocation_cursor-(heap+0x60000),
        file_events=file_events,virtual_directories=sorted(virtual_directories),
        path_conversions=sorted(path_conversions),error_flags=error_flags,
        dispatch=dict(surface_type_offset='0x1004',surface_type=2,builder_rva='0x6f9928',
                      constructor_rva='0x6e34e8',constructor_label='MtlEngineSdp',
                      original_dispatch_executed=True,original_constructor_completed=True,
                      builder_checkpoint_rva="0x6f9a38",builder_completed=True,builder_return=0,
                      verified_object_regions=[dict(offset="0x1b8",bytes=4376,source_kind=1),
                                               dict(offset="0x12f0",bytes=156,source_kind=2)],
                      verified_argument_source_kinds=[None,2,1,3,0,4,None]),
        original_stack_helpers_executed=True,memcpy_size_counts=dict(Counter(copies)),runtime_shims=shims,
        crt_initializers=["0xf267d0","0xf190e0","0xf1dce0","0xf2fa98","0xf1f750","0xf07dc0"],
        optional_api_queries=queries,fls_cleanup_callbacks_not_executed=fls_callbacks,
        builder_graph=builder_graph,ir_graph=dict(rva="0x70e910",completed=True,**ir_graph),
        verified_regions=[dict(offset=0,bytes=236,source_kind=2),dict(offset=240,bytes=34816,source_kind=3),
                          dict(offset=35056,bytes=4376,source_kind=1)],
        untouched_regions=[dict(offset=236,bytes=4),dict(offset=39432,bytes=1368)],
        limitations=['Collector uses a preallocated record; builder graph vectors execute original growth with modeled allocation',
                     'One captured ReLU input is translated under constructor defaults; full Windows frontend configuration is not reproduced',
                     'Windows APIs use a single-thread C-locale model; optional locale APIs are unavailable',
                     'CRT teardown is not executed and freed storage is not reused',
                     'Clock ticks are synthetic; no performance measurements are valid',
                     'Backend outputs are not a complete loadable; runtime packaging, firmware acceptance and inference remain unverified']),indent=2))


if __name__=='__main__':main()
