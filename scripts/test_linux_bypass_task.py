#!/usr/bin/env python3
"""Compile the exact native bypass phase against deterministic device failures."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('static int n1x_boot_bypass_task_once('):
                      source.index('static ssize_t bypass_task_store(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_task.h"
#include "n1x_dla_command.h"
#include "n1x_dla_response.h"
typedef uint32_t u32;
typedef __u64 u64;
typedef uint64_t dma_addr_t;
typedef uint32_t __le32;
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0
#define WRITE_ONCE(x,v) ((x)=(v))
#define READ_ONCE(x) (x)
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static struct {bool verified;} boot_ping;
static struct {bool registered;} boot_region5;
static struct {bool accepted;} boot_config_apply,boot_lpwr;
static struct {void *buffer;dma_addr_t dma;struct n1x_dla_response response;
 u32 pending,fence;bool attempted,response_valid,accepted,signal_seen,verified;int error;} boot_task;
static bool enable_bypass_task,machine_ok,allocation_fail,sent;
static unsigned char allocation[4096] __attribute__((aligned(256)));
static dma_addr_t allocation_address;
static int gate_error,mask_error,allocations,frees,barriers,io_calls,fail_at,cases;
static int stage,delays,post_ack_reads;
static u32 response_word,response_data,method_data,preexisting;
enum {NORMAL,COALESCED,NO_REPLY,ACCEPTANCE_ONLY,NO_FENCE,EXEC_ERROR,LATE_ERROR,DUPLICATE};
static int behavior;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&device && bytes==4096 && flags==GFP_KERNEL);allocations++;
 *address=allocation_address;memset(allocation,0xa5,sizeof(allocation));return allocation_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&device && bytes==4096 && buffer==allocation && address==allocation_address);
 assert(!boot_task.attempted);frees++;}
static void put64(unsigned char *p,u64 v) {for(int i=0;i<8;i++)p[i]=v>>(8*i);}
static void check_payload(void)
{unsigned char expected[4096]={0};expected[8]=2;expected[9]=0x44;expected[10]=96;
 expected[15]=1;expected[18]=60;put64(expected+21,allocation_address+256);expected[29]=1;
 expected[41]=1;expected[60]=64;expected[62]=14;expected[64]=0x80;
 put64(expected+65,allocation_address+768);expected[73]=0x78;expected[74]=0x56;
 expected[75]=0x34;expected[76]=0x12;put64(expected+256,allocation_address+512);
 expected[768]=0xef;expected[769]=0xbe;expected[770]=0xad;expected[771]=0xde;
 assert(!memcmp(allocation,expected,sizeof(expected)));}
static int n1x_boot_read(void *context,u32 offset,u32 *value)
{assert(context==&endpoint);int index=io_calls++;
 if(index==fail_at)return -EIO;
 if(offset==8){
  if(!sent)*value=preexisting;
  else if(behavior==NO_REPLY)*value=0;
  else if(stage==0)*value=behavior==COALESCED?0xc0:0x80;
  else if(behavior==ACCEPTANCE_ONLY)*value=0;
  else if(behavior==EXEC_ERROR||behavior==DUPLICATE)*value=0xc0;
  else if(behavior==LATE_ERROR)*value=++post_ack_reads==1?0x40:0xc0;
  else *value=0x40;
 }else if(offset==0x68)*value=0;
 else if(offset==0x40)*value=stage && behavior!=DUPLICATE?0x8007070a:response_word;
 else {assert(offset==0x44);*value=response_data;}
 return 0;}
static int n1x_boot_write(void *context,u32 offset,u32 value)
{assert(context==&endpoint);int index=io_calls++;
 if(offset==0x6c)method_data=value;
 else if(offset==0x68){assert(value==0x307 && method_data==allocation_address>>8);
 assert(barriers==1 && boot_task.attempted && !frees);check_payload();sent=true;}
 else if(offset==0xd24)assert(value==128);
 else if(offset==4){assert(value==64||value==128);
 if(value==64)assert(boot_task.accepted && boot_task.signal_seen && boot_task.fence==0x12345678);}
 else {assert(offset==0x40 && value==N1X_DLA_RESPONSE_ACK);stage++;}
 return index==fail_at?-EIO:0;}
static void n1x_boot_delay(void *context,u32 interval)
{assert(context==&endpoint && interval==1000);delays++;}
static void dma_wmb(void) {barriers++;}
static void dma_rmb(void)
{if((stage||behavior==COALESCED) && behavior!=NO_FENCE)
 *(u32 *)(allocation+768)=0x12345678;}
'''
    harness += function
    harness += r'''
static void reset(void)
{boot_sroot=boot_dla=&endpoint;boot_ping.verified=boot_region5.registered=true;
 boot_config_apply.accepted=boot_lpwr.accepted=enable_bypass_task=machine_ok=true;
 allocation_fail=sent=false;allocation_address=0x100002000ULL;
 gate_error=mask_error=allocations=frees=barriers=io_calls=stage=delays=post_ack_reads=0;fail_at=-1;
 response_word=0x702;response_data=allocation_address>>8;method_data=preexisting=0;behavior=NORMAL;
 memset(&boot_task,0,sizeof(boot_task));}
static void check(int expected,int allocs,int free_count,bool attempted,bool verified)
{assert(n1x_boot_bypass_task_once()==expected);assert(allocations==allocs && frees==free_count);
 assert(boot_task.attempted==attempted && boot_task.verified==verified);
 assert(!!boot_task.buffer==attempted);
 if(attempted){assert(n1x_boot_bypass_task_once()==-EALREADY);assert(allocations==1);}cases++;}
int main(void)
{reset();check(0,1,0,true,true);int baseline=io_calls;assert(boot_task.accepted && boot_task.signal_seen);
 reset();behavior=COALESCED;check(0,1,0,true,true);
 reset();enable_bypass_task=false;check(-EPERM,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,false,false);
 reset();boot_ping.verified=false;check(-ENODEV,0,0,false,false);
 reset();boot_region5.registered=false;check(-ENODEV,0,0,false,false);
 reset();boot_config_apply.accepted=false;check(-ENODEV,0,0,false,false);
 reset();boot_lpwr.accepted=false;check(-ENODEV,0,0,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,false,false);
 reset();mask_error=-EIO;check(-EIO,0,0,false,false);
 reset();allocation_fail=true;check(-ENOMEM,1,0,false,false);
 for(int kind=0;kind<4;kind++){reset();allocation_address=kind==0?0:kind==1?0x100002001ULL:
 kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;check(-ERANGE,1,1,false,false);}
 for(u32 pending=0x40;pending<=0xc0;pending+=0x40){reset();preexisting=pending;check(-EBUSY,0,0,false,false);}
 for(int at=0;at<baseline;at++){reset();fail_at=at;check(-EIO,at?1:0,0,at!=0,false);assert(io_calls==at+1);}
 for(int mode=NO_REPLY;mode<=NO_FENCE;mode++){reset();behavior=mode;
 check(-ETIMEDOUT,1,0,true,false);assert(delays==5000);
 if(mode==ACCEPTANCE_ONLY)assert(boot_task.accepted && boot_task.fence==0x12345678 && !boot_task.signal_seen);
 if(mode==NO_FENCE)assert(boot_task.accepted && boot_task.signal_seen && boot_task.fence==0xdeadbeef);}
 reset();behavior=EXEC_ERROR;check(-EREMOTEIO,1,0,true,false);
 assert(boot_task.response_valid && boot_task.response.word==0x8007070a && boot_task.accepted);
 reset();behavior=LATE_ERROR;check(-EREMOTEIO,1,0,true,false);assert(boot_task.signal_seen && post_ack_reads>=2);
 reset();behavior=DUPLICATE;check(-EPROTO,1,0,true,false);
 reset();response_word=0x90701;check(-EREMOTEIO,1,0,true,false);assert(!boot_task.accepted);
 reset();response_data++;check(-EPROTO,1,0,true,false);
 reset();response_word=0x602;check(-EPROTO,1,0,true,false);
 reset();response_word=0x1000702;check(-EPROTO,1,0,true,false);
 printf("%d native bypass-task lifecycle cases; %d I/O fault positions; acceptance/fence/SWGEN0/error separation passed; no hardware access\n",cases,baseline);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-linux-bypass-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_task.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    main()
