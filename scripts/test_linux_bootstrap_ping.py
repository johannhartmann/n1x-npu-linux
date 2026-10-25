#!/usr/bin/env python3
"""Compile the exact kernel ping phase with real C method sender and fake OS/MMIO."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('static int n1x_boot_ping_once('):
                      source.index('/* Caller holds boot_lock; snapshot export')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_boot.h"
#include "n1x_dla_command.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_dla,*boot_sroot;
static struct n1x_dla_sroot_load_result boot_result;
static bool enable_ping,machine_ok,boot_attempted;
static struct {void *buffer;dma_addr_t dma;u32 owned,mask_before,masked,word;
 bool attempted,verified;int error;} boot_ping;
static unsigned char allocation[4096];
static dma_addr_t allocation_address;
static int allocations,frees,io_calls,fail_at,delays,word_reads,cases;
static int gate_error,mask_error,complete_after,method_busy,send_count;
static bool allocation_fail,method_sent;
static u32 current_mask,owned_mask,method_data;
static int barriers,read_barriers;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&device && bytes==4096 && flags==GFP_KERNEL);allocations++;*address=allocation_address;
 memset(allocation,0xa5,sizeof(allocation));return allocation_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&device && bytes==4096 && buffer==allocation && address==allocation_address);
 assert(!boot_ping.attempted);frees++;}
static int n1x_boot_read(void *context,u32 offset,u32 *value)
{assert(context==&endpoint);int index=io_calls++;
 if(index==fail_at)return -EIO;
 if(offset==0xd2c)*value=owned_mask;
 else if(offset==0xd28)*value=current_mask;
 else {assert(offset==0x68);*value=method_busy-->0?1U<<16:0;}
 return 0;}
static int n1x_boot_write(void *context,u32 offset,u32 value)
{assert(context==&endpoint);int index=io_calls++;
 /* Model failure AFTER a possible side effect, not a guaranteed no-op. */
 if(offset==0xd24){assert(value==(owned_mask&current_mask));current_mask&=~value;}
 else if(offset==0x6c)method_data=value;
 else {assert(offset==0x68 && value==1 && method_data==allocation_address>>8);
 assert(barriers==1 && boot_ping.attempted && !frees);
 assert(*(u32 *)allocation==0x11111111);method_sent=true;send_count++;
 if(complete_after==0)*(u32 *)allocation=0x44444444;}
 return index==fail_at?-EIO:0;}
static void n1x_boot_delay(void *context,u32 interval)
{assert(context==&endpoint && interval==1000);
 if(method_sent){delays++;if(delays==complete_after)*(u32 *)allocation=0x44444444;}}
static u32 read_once_word(const __le32 *pointer)
{assert(pointer==(__le32 *)allocation);word_reads++;return *pointer;}
#define READ_ONCE(x) read_once_word(&(x))
static void dma_wmb(void) {barriers++;}
static void dma_rmb(void) {read_barriers++;}
'''
    harness += function
    harness += r'''
static void reset(void)
{enable_ping=machine_ok=boot_attempted=true;boot_dla=boot_sroot=&endpoint;
 memset(&boot_result,0,sizeof(boot_result));boot_result.reply_valid=boot_result.idle_observed=1;
 memset(&boot_ping,0,sizeof(boot_ping));allocation_address=0x100000000ULL;
 allocations=frees=io_calls=delays=word_reads=gate_error=mask_error=0;
 barriers=read_barriers=method_busy=send_count=0;fail_at=-1;complete_after=0;
 allocation_fail=method_sent=false;current_mask=0x1f85;owned_mask=0x1d0;}
static void check(int expected,int allocs,int free_count,bool attempted,bool verified)
{assert(n1x_boot_ping_once()==expected);assert(allocations==allocs && frees==free_count);
 assert(boot_ping.attempted==attempted && boot_ping.verified==verified);
 assert(!!boot_ping.buffer==attempted);assert(read_barriers==(int)verified);cases++;}
int main(void)
{reset();enable_ping=false;check(-EPERM,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_attempted=false;check(-ENODEV,0,0,false,false);
 reset();boot_result.reply_valid=0;check(-ENODEV,0,0,false,false);
 reset();boot_result.idle_observed=0;check(-ENODEV,0,0,false,false);
 reset();boot_result.reply.status=1;check(-ENODEV,0,0,false,false);
 reset();boot_result.reply.detail=1;check(-ENODEV,0,0,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,false,false);
 reset();mask_error=-EIO;check(-EIO,0,0,false,false);
 reset();allocation_fail=true;check(-ENOMEM,1,0,false,false);
 for(int kind=0;kind<4;kind++){reset();
 allocation_address=kind==0?0:kind==1?0x100000001ULL:kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;
 check(-ERANGE,1,1,false,false);}
 for(int at=0;at<6;at++){reset();fail_at=at;check(-EIO,1,at<2,at>=2,false);
 assert(io_calls==at+1);if(at>=2){assert(n1x_boot_ping_once()==-EALREADY);assert(allocations==1);}}
 for(int delay=0;delay<4;delay++){reset();complete_after=delay==0?0:delay==1?1:delay==2?4999:5000;
 check(0,1,0,true,true);assert(delays==complete_after && word_reads==complete_after+1);
 assert(send_count==1 && boot_ping.masked==0x180 && boot_ping.word==0x44444444);
 assert(n1x_boot_ping_once()==-EALREADY && allocations==1);}
 reset();complete_after=5001;check(-ETIMEDOUT,1,0,true,false);
 assert(delays==5000 && word_reads==5001 && boot_ping.word==0x11111111);
 reset();method_busy=5001;check(-ETIMEDOUT,1,0,true,false);assert(!method_sent && !word_reads);
 reset();allocation_address=DMA_BIT_MASK(36)-4095;check(0,1,0,true,true);
 printf("%d Linux DMA-word ping lifecycle cases passed with real method sender; no hardware access\n",cases);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-bootstrap-ping-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_command.c'), '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    main()
