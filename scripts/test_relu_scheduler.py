#!/usr/bin/env python3
"""Run the compiled ReLU task through unmodified HAL-1 scheduler/IRQ code.

DMA, engine registers and IRQ delivery are RAM models. No arithmetic engine
is emulated: even the control-completion cases leave the output poisoned.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

from unicorn import UC_HOOK_CODE,UC_HOOK_MEM_READ
from unicorn import riscv_const as regs
from diagnostic_fixture import build_relu
from emulate_profile1_commands import run,ROOT,HOST,MMIO,ACK,PROFILE1_HASH
from test_task_descriptor import Args
from test_model_addresses import resolve_relu
from test_model_contents import initialize_relu
from verify_relu_loadable import LOADABLE_HASH


def main():
    firmware=(ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    data=(ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(firmware).hexdigest()==PROFILE1_HASH
    assert hashlib.sha256(data).hexdigest()==LOADABLE_HASH
    network=(ROOT/'results/windows-final-backend-output/kind-3.bin').read_bytes()
    assert hashlib.sha256(network).hexdigest()=='86f4866d1a6a859b697a8a97548080bcfdc3ef21f06e2f9cb90efc44b75ae781'
    input_data=(ROOT/'results/relu-probe/input-chw16-fp16.bin').read_bytes()
    oracle=json.loads((ROOT/'results/relu-probe/manifest.json').read_text())
    assert len(input_data)==3136
    assert hashlib.sha256(input_data).hexdigest()==oracle['files']['input-chw16-fp16.bin']['sha256']=='73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45'
    with tempfile.TemporaryDirectory(prefix='n1x-relu-scheduler-') as tmp:
        library=Path(tmp)/'task.so'
        subprocess.run(shlex.split(os.environ.get('CC','cc'))+[
            '-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-I'+str(ROOT/'include'),
            str(ROOT/'driver/n1x_dla_task.c'),str(ROOT/'driver/n1x_dla_model.c'),
            '-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library))
    fenced=lib.n1x_dla_build_fenced_task_v2
    fenced.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(Args),ctypes.c_uint64,ctypes.c_uint32]
    address_list=lib.n1x_dla_build_address_list
    address_list.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint16]
    cases=[]
    for irq in (0x400,0x800):
        for engine_status in (0,1,4,5):
            reference,addresses,layout=build_relu(data,HOST)
            payload=initialize_relu(lib,data,layout)
            assert payload==reference
            assert len(addresses)==8 and addresses[0]==layout[3]['address']
            resolved=resolve_relu(lib,data,layout)
            assert [b.address for b in resolved]==addresses
            native_addresses=(ctypes.c_uint64*len(resolved))(*[b.address for b in resolved])
            wire=ctypes.create_string_buffer(256)
            assert address_list(wire,256,native_addresses,len(resolved))==0
            assert wire.raw==struct.pack('<8Q',*addresses)+bytes(192)
            for memory,value in ((1,input_data),(2,b'\xa5'*3136)):
                start=layout[memory]['offset'];payload[start:start+3136]=value
            task=ctypes.create_string_buffer(256);args=Args(0,HOST+256,1000000,8)
            assert fenced(task,256,ctypes.byref(args),HOST+768,0x12345678)==0
            payload[:256]=task.raw
            payload[256:512]=wire.raw
            struct.pack_into('<I',payload,768,0xdeadbeef)
            live=[]
            def response(u):
                live[:]=[u]
                for offset in (8,0xd28):
                    value=struct.unpack('<I',u.mem_read(MMIO+offset,4))[0]
                    u.mem_write(MMIO+offset,struct.pack('<I',value&~128))
                u.mem_write(MMIO+0x40,struct.pack('<I',ACK))
            result=run(firmware,method=0x307,host_payload=payload,host_handler=response,
                       boot_from_reset=True,via_trap=True,dma_register_model=True,
                       resume_scheduler=True)
            assert result['returned'] and result['scheduler_wfi'] and result['wfi_return_address']==0x101424
            assert result['function_stubs']==[] and result['firmware_code_unmodified']
            assert result['interrupts']==[(0x702,HOST>>8)] and not result['task_interrupts']
            u=live[0]
            def word(address):return struct.unpack('<I',u.mem_read(address,4))[0]
            context=word(0x1862bc)+40;internal=word(context);local_network=word(context+8)
            assert bytes(u.mem_read(local_network,256))==network
            assert word(internal+40)==1 and word(0x1862a4)==1 and word(HOST+768)==0xdeadbeef
            assert any(d['source']==addresses[4]+256 and d['bytes']==64 for d in result['dma_commands'])
            assert any(addresses[3]<=d['source']<addresses[3]+30464 for d in result['dma_commands'])
            # Original instruction processing installs these seven address pairs
            # into the RAM register model. Do not infer a general register ABI.
            for index,address in enumerate(addresses[:7]):
                assert (0x2505c+8*index,4,address&0xffffffff) in result['scheduler_mmio_writes']
                assert (0x25060+8*index,4,address>>32) in result['scheduler_mmio_writes']
            entered=[];status_reads=[]
            def handler_entry(_,pc,size,user):entered.append(pc)
            def status_read(_,access,address,size,value,user):status_reads.append(word(address))
            u.hook_add(UC_HOOK_CODE,handler_entry,begin=0x1025e6,end=0x1025e6)
            u.hook_add(UC_HOOK_MEM_READ,status_read,begin=MMIO+0x3054,end=MMIO+0x3057)
            before_returns=len(result['trap_returns'])
            assert word(MMIO+0xd28)&irq
            u.mem_write(MMIO+0x3054,struct.pack('<I',engine_status))
            u.mem_write(MMIO+8,struct.pack('<I',word(MMIO+8)|irq))
            status=u.reg_read(regs.UC_RISCV_REG_MSTATUS)
            u.reg_write(regs.UC_RISCV_REG_MSTATUS,(status&~0x1888)|0x1800|(0x80 if status&8 else 0))
            u.reg_write(regs.UC_RISCV_REG_MCAUSE,0x8000000b)
            u.reg_write(regs.UC_RISCV_REG_MEPC,0x101424)
            u.emu_start(u.reg_read(regs.UC_RISCV_REG_MTVEC),0x40000000,count=200000)
            assert entered==[0x1025e6] and len(result['trap_returns'])==before_returns+1
            assert u.reg_read(regs.UC_RISCV_REG_PC)==0x102194 and not word(MMIO+8)&irq
            assert status_reads[:2]==[engine_status,engine_status]
            completed=bool(engine_status&1)
            assert word(internal+40)==(0 if completed else 1)
            assert word(0x1862a4)==(0 if completed else 1)
            assert word(HOST+768)==(0x12345678 if completed else 0xdeadbeef)
            assert len(result['task_interrupts'])==int(completed)
            assert word(0x180114)==(0 if completed else 1)
            assert bytes(u.mem_read(layout[1]['address'],3136))==input_data
            assert bytes(u.mem_read(layout[2]['address'],3136))==b'\xa5'*3136
            assert bytes(u.mem_read(0x100000,36864))==firmware[512:512+36864]
            if completed:
                assert any(d['direction']=='write' and d['destination']==HOST+768 and d['bytes']==4
                           for d in result['dma_commands'])
            cases.append(dict(irq=hex(irq),modeled_engine_status=engine_status,
                original_handler_entered=True,original_trap_returned=True,
                original_postaction_fence_written=completed,task_irq_asserted=completed,
                remaining_records=word(internal+40),output_unchanged=True,
                firmware_code_unmodified=True,status_reads=status_reads))
    print(json.dumps(dict(passed=True,hardware_access=False,loadable_sha256=LOADABLE_HASH,
        firmware_sha256=PROFILE1_HASH,input_sha256=hashlib.sha256(input_data).hexdigest(),
        task_local_address_ids=[3,1,2,4,5,6,7,8],native_content_initialization=True,
        native_address_resolution_and_serialization=True,cases=cases,
        limitations=['Synchronous RAM DMA, modeled engine status and injected firmware IRQs',
                     'No engine arithmetic is emulated; the poisoned output remains unchanged',
                     'Control completion is not numerical inference or physical NPU execution',
                     'Firmware-local IRQ bits do not establish physical GIC routing',
                     'Runtime initialization and opaque SROOT executable equivalence remain unverified']),indent=2))


if __name__=='__main__':main()
