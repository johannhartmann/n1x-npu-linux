#!/usr/bin/env python3
"""Verify exact Linux region-5 setup against a full original Windows buffer."""
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import pefile
from emulate_region5_setup import windows, ROOT, HOST, KMD_SHA256


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    original = windows(pefile.PE(str(path), fast_load=True))
    assert original['builder_status'] == 0
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    constants = source[source.index('#define N1X_REGION5_ALLOC'):source.index('static bool enable_upload;')]
    function = source[source.index('static int n1x_boot_init_region5_once('):
                      source.index('static ssize_t init_region5_store(')]
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
'''+constants+r'''
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static struct {bool verified;} boot_ping;
static struct {void *buffer;dma_addr_t dma;struct n1x_dla_control_result transaction;
 bool attempted,registered;int error;} boot_region5;
static bool enable_region5,machine_ok,allocation_fail,stale,sent;
static unsigned char allocation[N1X_REGION5_ALLOC] __attribute__((aligned(256)));
static unsigned char windows_reference[N1X_REGION5_ALLOC];
static dma_addr_t allocation_address;
static int gate_error,mask_error,allocations,frees,barriers,io_calls,fail_at,cases;
static u32 response_word,response_data,method_data;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&device && bytes==N1X_REGION5_ALLOC && flags==GFP_KERNEL);allocations++;
 *address=allocation_address;memset(allocation,0xa5,sizeof(allocation));return allocation_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&device && bytes==N1X_REGION5_ALLOC && buffer==allocation && address==allocation_address);
 assert(!boot_region5.attempted);frees++;}
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
 else if(offset==0x68){assert(value==0x30b && method_data==allocation_address>>8);
 assert(barriers==1 && boot_region5.attempted && !frees);
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
{boot_sroot=boot_dla=&endpoint;boot_ping.verified=true;enable_region5=machine_ok=true;
 allocation_fail=stale=sent=false;allocation_address=HOST_ADDRESS;
 gate_error=mask_error=allocations=frees=barriers=io_calls=0;fail_at=-1;
 response_word=0xb02;response_data=allocation_address>>8;method_data=0;
 memset(&boot_region5,0,sizeof(boot_region5));}
static void check(int expected,int allocs,int free_count,bool attempted,bool registered)
{assert(n1x_boot_init_region5_once()==expected);assert(allocations==allocs && frees==free_count);
 assert(boot_region5.attempted==attempted && boot_region5.registered==registered);
 assert(!!boot_region5.buffer==attempted);
 if(attempted){assert(n1x_boot_init_region5_once()==-EALREADY);assert(allocations==1);}cases++;}
int main(int argc,char **argv)
{assert(argc==2);FILE *file=fopen(argv[1],"rb");assert(file);
 assert(fread(windows_reference,1,sizeof(windows_reference),file)==sizeof(windows_reference));
 assert(fgetc(file)==EOF);fclose(file);
 assert(N1X_REGION5_BYTES==0xfffd8 && N1X_REGION5_OFFSET==16 && N1X_REGION5_STRIDE==52);
 reset();enable_region5=false;check(-EPERM,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_ping.verified=false;check(-ENODEV,0,0,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,false,false);
 reset();mask_error=-EIO;check(-EIO,0,0,false,false);
 reset();allocation_fail=true;check(-ENOMEM,1,0,false,false);
 for(int kind=0;kind<4;kind++){reset();allocation_address=kind==0?0:kind==1?HOST_ADDRESS+1:
 kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;check(-ERANGE,1,1,false,false);}
 for(int at=0;at<10;at++){reset();fail_at=at;check(-EIO,1,0,true,false);assert(io_calls==at+1);}
 reset();stale=true;check(-EBUSY,1,0,true,false);assert(!boot_region5.transaction.method_attempted);
 reset();response_word=0x110b01;check(-EREMOTEIO,1,0,true,false);assert(boot_region5.transaction.response_valid);
 reset();response_data++;check(-EPROTO,1,0,true,false);assert(boot_region5.transaction.response_valid);
 reset();check(0,1,0,true,true);assert(boot_region5.transaction.response_valid);
 printf("%d region-5 lifecycle cases and full Windows 1-MiB buffer equivalence passed; no hardware access\n",cases);
 return 0;}
'''
    harness = '#define HOST_ADDRESS '+hex(HOST)+'ULL\n'+harness
    with tempfile.TemporaryDirectory(prefix='n1x-region5-linux-') as tmp:
        code, executable, reference = Path(tmp)/'test.c', Path(tmp)/'test', Path(tmp)/'windows.bin'
        code.write_text(harness)
        reference.write_bytes(original['payload'])
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_control.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable), str(reference)], check=True)


if __name__ == '__main__':
    main()
