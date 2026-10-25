#!/usr/bin/env python3
"""Test exact Linux snapshot lifecycle with the real C control/response pipeline."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('static int n1x_boot_read_config_once('):
                      source.index('static ssize_t read_config_store(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_control.h"
typedef uint8_t u8;
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
static struct {void *buffer;dma_addr_t dma;struct n1x_dla_control_result transaction;
 u8 data[252];bool attempted,valid;int error;} boot_config;
static bool enable_config_snapshot,machine_ok,allocation_fail,stale,sent;
static u8 allocation[4096] __attribute__((aligned(256)));
static dma_addr_t allocation_address;
static int gate_error,mask_error,allocations,frees,barriers,read_barriers,io_calls,fail_at,cases;
static u32 response_word,response_data,method_data;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&device && bytes==4096 && flags==GFP_KERNEL);allocations++;*address=allocation_address;
 memset(allocation,0xa5,sizeof(allocation));return allocation_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&device && bytes==4096 && buffer==allocation && address==allocation_address);
 assert(!boot_config.attempted);frees++;}
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
 else if(offset==0x68){assert(value==0x316 && method_data==allocation_address>>8);
 assert(barriers==1 && boot_config.attempted && !frees);
 for(size_t i=0;i<sizeof(allocation);i++)assert(allocation[i]==0);
 for(size_t i=0;i<252;i++)allocation[i]=(i*13+2)&255;sent=true;}
 else if(offset==0xd24||offset==4)assert(value==128);
 else assert(offset==0x40 && value==N1X_DLA_RESPONSE_ACK);
 return index==fail_at?-EIO:0;}
static void n1x_boot_delay(void *context,u32 interval)
{(void)context;(void)interval;assert(!"unexpected delay");}
static void dma_wmb(void) {barriers++;}
static void dma_rmb(void) {read_barriers++;}
'''
    harness += function
    harness += r'''
static void reset(void)
{boot_sroot=boot_dla=&endpoint;boot_ping.verified=true;enable_config_snapshot=machine_ok=true;
 allocation_fail=stale=sent=false;allocation_address=0x100001000ULL;
 gate_error=mask_error=allocations=frees=barriers=read_barriers=io_calls=0;fail_at=-1;
 response_word=0x1602;response_data=allocation_address>>8;method_data=0;
 memset(&boot_config,0,sizeof(boot_config));}
static void check(int expected,int allocs,int free_count,bool attempted,bool valid)
{assert(n1x_boot_read_config_once()==expected);assert(allocations==allocs && frees==free_count);
 assert(boot_config.attempted==attempted && boot_config.valid==valid);
 assert(!!boot_config.buffer==attempted && read_barriers==(int)valid);
 for(size_t i=0;i<sizeof(boot_config.data);i++)assert(boot_config.data[i]==(valid?((i*13+2)&255):0));
 if(attempted){assert(n1x_boot_read_config_once()==-EALREADY);assert(allocations==1);}cases++;}
int main(void)
{reset();enable_config_snapshot=false;check(-EPERM,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_ping.verified=false;check(-ENODEV,0,0,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,false,false);
 reset();mask_error=-EIO;check(-EIO,0,0,false,false);
 reset();allocation_fail=true;check(-ENOMEM,1,0,false,false);
 for(int kind=0;kind<4;kind++){reset();
 allocation_address=kind==0?0:kind==1?0x100001001ULL:kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;
 check(-ERANGE,1,1,false,false);}
 for(int at=0;at<10;at++){reset();fail_at=at;check(-EIO,1,0,true,false);assert(io_calls==at+1);}
 reset();stale=true;check(-EBUSY,1,0,true,false);assert(!boot_config.transaction.method_attempted);
 reset();response_word=0x111601;check(-EREMOTEIO,1,0,true,false);assert(boot_config.transaction.response_valid);
 reset();response_data++;check(-EPROTO,1,0,true,false);assert(boot_config.transaction.response_valid);
 reset();check(0,1,0,true,true);assert(boot_config.transaction.response_valid);
 printf("%d config-snapshot lifecycle cases with real C control/ACK pipeline passed; no hardware access\n",cases);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-config-snapshot-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_dla_control.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    main()
