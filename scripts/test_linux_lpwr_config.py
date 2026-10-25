#!/usr/bin/env python3
"""Verify native LPWR import against the original Windows fallback payload."""
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import struct
import tempfile
import pefile
from test_command23_builder import windows, ROOT, HOST, KMD_SHA256
from emulate_profile1_commands import run, MMIO, ACK, PROFILE1_HASH


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    original = windows(pefile.PE(str(path), fast_load=True), 1, 1, 4)
    assert original['status'] == 0
    reference_bytes = original['record'] + original['tail']
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('static int n1x_boot_apply_lpwr_once('):
                      source.index('static ssize_t apply_lpwr_store(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_control.h"
#include "n1x_dla_command.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0

struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static struct {bool verified;} boot_ping;
static struct {bool registered;} boot_region5;
static struct {bool accepted;} boot_config_apply;
static struct {void *buffer;dma_addr_t dma;struct n1x_dla_control_result transaction;
 bool attempted,accepted;int error;} boot_lpwr;
static bool enable_lpwr_config,machine_ok,allocation_fail,stale,sent;
static unsigned char allocation[4096] __attribute__((aligned(256)));
static unsigned char windows_reference[4096];
static dma_addr_t allocation_address;
static int gate_error,mask_error,allocations,frees,barriers,io_calls,fail_at,cases;
static u32 response_word,response_data,method_data;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&device && bytes==4096 && flags==GFP_KERNEL);allocations++;
 *address=allocation_address;memset(allocation,0xa5,sizeof(allocation));return allocation_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&device && bytes==4096 && buffer==allocation && address==allocation_address);
 assert(!boot_lpwr.attempted);frees++;}
static int n1x_boot_read(void *context,u32 offset,u32 *value)
{assert(context==&endpoint);int index=io_calls++;
 if(index==fail_at)return -EIO;
 if(offset==8)*value=sent||stale?128:0;
 else if(offset==0x68)*value=0;
 else if(offset==0x40)*value=response_word;
 else {assert(offset==0x44);*value=response_data;}
 return 0;}
static int n1x_boot_write(void *context,u32 offset,u32 value)
{assert(context==&endpoint);int index=io_calls++;
 if(offset==0x6c)method_data=value;
 else if(offset==0x68){assert(value==0x317 && method_data==allocation_address>>8);
 assert(barriers==1 && boot_lpwr.attempted && !frees);
 assert(!memcmp(allocation,windows_reference,sizeof(allocation)));sent=true;}
 else if(offset==0xd24||offset==4)assert(value==128);
 else assert(offset==0x40 && value==N1X_DLA_RESPONSE_ACK);
 return index==fail_at?-EIO:0;}
static void n1x_boot_delay(void *context,u32 interval)
{(void)context;(void)interval;assert(!"unexpected delay");}
static void dma_wmb(void) {barriers++;}
'''
    harness += function
    harness += r'''
static void reset(void)
{boot_sroot=boot_dla=&endpoint;boot_ping.verified=boot_region5.registered=boot_config_apply.accepted=true;enable_lpwr_config=machine_ok=true;
 allocation_fail=stale=sent=false;allocation_address=HOST_ADDRESS;
 gate_error=mask_error=allocations=frees=barriers=io_calls=0;fail_at=-1;
 response_word=0x1702;response_data=allocation_address>>8;method_data=0;
 memset(&boot_lpwr,0,sizeof(boot_lpwr));}
static void check(int expected,int allocs,int free_count,bool attempted,bool accepted)
{assert(n1x_boot_apply_lpwr_once()==expected);assert(allocations==allocs && frees==free_count);
 assert(boot_lpwr.attempted==attempted && boot_lpwr.accepted==accepted);
 assert(!!boot_lpwr.buffer==attempted);
 if(attempted){assert(n1x_boot_apply_lpwr_once()==-EALREADY);assert(allocations==1);}cases++;}
int main(int argc,char **argv)
{assert(argc==2);FILE *file=fopen(argv[1],"rb");assert(file);
 assert(fread(windows_reference,1,sizeof(windows_reference),file)==sizeof(windows_reference));
 assert(fgetc(file)==EOF);fclose(file);
 reset();enable_lpwr_config=false;check(-EPERM,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_ping.verified=false;check(-ENODEV,0,0,false,false);
 reset();boot_region5.registered=false;check(-ENODEV,0,0,false,false);
 reset();boot_config_apply.accepted=false;check(-ENODEV,0,0,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,false,false);
 reset();mask_error=-EIO;check(-EIO,0,0,false,false);
 reset();allocation_fail=true;check(-ENOMEM,1,0,false,false);
 for(int kind=0;kind<4;kind++){reset();allocation_address=kind==0?0:kind==1?HOST_ADDRESS+1:
 kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;check(-ERANGE,1,1,false,false);}
 for(int at=0;at<10;at++){reset();fail_at=at;check(-EIO,1,0,true,false);assert(io_calls==at+1);}
 reset();stale=true;check(-EBUSY,1,0,true,false);assert(!boot_lpwr.transaction.method_attempted);
 reset();response_word=0x111701;check(-EREMOTEIO,1,0,true,false);assert(boot_lpwr.transaction.response_valid);
 reset();response_data++;check(-EPROTO,1,0,true,false);assert(boot_lpwr.transaction.response_valid);
 reset();check(0,1,0,true,true);assert(boot_lpwr.transaction.response_valid);
 printf("%d LPWR lifecycle cases and full Windows 4-KiB fallback buffer equivalence passed; no hardware access\n",cases);
 return 0;}
'''
    harness = '#define HOST_ADDRESS '+hex(HOST)+'ULL\n'+harness
    with tempfile.TemporaryDirectory(prefix='n1x-lpwr-linux-') as tmp:
        code, executable, reference = Path(tmp)/'test.c', Path(tmp)/'test', Path(tmp)/'windows.bin'
        code.write_text(harness)
        reference.write_bytes(reference_bytes)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_control.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable), str(reference)], check=True)


    # Retain one original HAL-1 instance across set/get; erase host data before
    # export, so equality cannot be explained by the initial host payload.
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    payload = bytearray(reference_bytes)
    commands = iter((23, 24))
    local, exported = [], []

    def submit(u):
        command = next(commands)
        u.mem_write(MMIO+0x68, struct.pack('<II', 0x300 | command, HOST >> 8))

    def response(u):
        local.append(bytes(u.mem_read(0x186254, 40)))
        if len(local) == 1:
            assert local[0] == reference_bytes[:40]
            payload[:] = bytes(4096)
        else:
            exported.append(bytes(u.mem_read(HOST, 4096)))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))

    result = run(blob, sequence=(0, 0), host_payload=payload, host_submit=submit,
                 host_handler=response, boot_from_reset=True, via_trap=True,
                 dma_register_model=True)
    assert result['returned'] and result['firmware_code_unmodified']
    assert result['function_stubs'] == [] and len(result['trap_returns']) == 2
    assert result['interrupts'] == [(0x1702, HOST >> 8), (0x1802, HOST >> 8)]
    assert len(result['dma_commands']) == 20
    assert local == [reference_bytes[:40]]*2 and exported == [reference_bytes]
    print('Same-instance original HAL-1 command 23/24 fallback roundtrip passed; 20 modeled DMA words, no hardware access')


if __name__ == '__main__':
    main()
