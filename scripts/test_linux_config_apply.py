#!/usr/bin/env python3
"""Exercise exact Linux config import using real prepare/control/send/ACK code."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('static int n1x_boot_apply_config_once('):
                      source.index('static ssize_t apply_config_store(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_command.h"
#include "n1x_dla_control.h"
typedef uint32_t u32;
typedef uint64_t u64;
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static struct {bool verified;} boot_ping;
static struct {bool registered;} boot_region5;
static struct {void *buffer;u64 dma;bool valid;unsigned char data[252];} boot_config;
static struct {struct n1x_dla_control_result transaction;bool attempted,accepted;int error;} boot_config_apply;
static bool enable_config_apply,machine_ok,stale,sent;
static unsigned char retained_buffer[4096] __attribute__((aligned(256)));
static unsigned char original[4096];
static int gate_error,barriers,io_calls,fail_at,cases;
static u32 response_word,response_data,method_data;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
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
 else if(offset==0x68){assert(value==0x315 && method_data==boot_config.dma>>8);
 assert(barriers==1 && boot_config_apply.attempted);
 for(size_t i=0;i<sizeof(retained_buffer);i++)
  assert(retained_buffer[i]==(i==1?2:i==50?1:original[i]));sent=true;}
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
{boot_sroot=boot_dla=&endpoint;boot_ping.verified=boot_region5.registered=true;
 enable_config_apply=machine_ok=true;stale=sent=false;gate_error=barriers=io_calls=0;fail_at=-1;
 for(size_t i=0;i<sizeof(original);i++)original[i]=i&255;
 memcpy(retained_buffer,original,sizeof(original));
 boot_config.buffer=retained_buffer;boot_config.dma=0x100002000ULL;boot_config.valid=true;
 memcpy(boot_config.data,original,sizeof(boot_config.data));
 response_word=0x1502;response_data=boot_config.dma>>8;method_data=0;
 memset(&boot_config_apply,0,sizeof(boot_config_apply));}
static void check(int expected,bool attempted,bool accepted)
{void *saved=boot_config.buffer;u64 address=boot_config.dma;
 assert(n1x_boot_apply_config_once()==expected);
 assert(boot_config_apply.attempted==attempted && boot_config_apply.accepted==accepted);
 assert(boot_config.buffer==saved && boot_config.dma==address);
 assert(!memcmp(boot_config.data,original,sizeof(boot_config.data)));
 if(attempted){assert(n1x_boot_apply_config_once()==-EALREADY);}
 else assert(!memcmp(retained_buffer,original,sizeof(original)));
 cases++;}
int main(void)
{reset();enable_config_apply=false;check(-EPERM,false,false);
 reset();machine_ok=false;check(-EPERM,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,false,false);
 reset();boot_dla=NULL;check(-ENODEV,false,false);
 reset();boot_ping.verified=false;check(-ENODEV,false,false);
 reset();boot_region5.registered=false;check(-ENODEV,false,false);
 reset();boot_config.valid=false;check(-ENODEV,false,false);
 reset();boot_config.buffer=NULL;check(-ENODEV,false,false);
 reset();gate_error=-EOPNOTSUPP;check(-EOPNOTSUPP,false,false);
 for(int kind=0;kind<4;kind++){reset();boot_config.dma=kind==0?0:kind==1?0x100002001ULL:
 kind==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-255;check(-ERANGE,false,false);}
 for(int at=0;at<10;at++){reset();fail_at=at;check(-EIO,true,false);assert(io_calls==at+1);}
 reset();stale=true;check(-EBUSY,true,false);assert(!boot_config_apply.transaction.method_attempted);
 reset();response_word=0x111501;check(-EREMOTEIO,true,false);assert(boot_config_apply.transaction.response_valid);
 reset();response_data++;check(-EPROTO,true,false);assert(boot_config_apply.transaction.response_valid);
 reset();check(0,true,true);assert(boot_config_apply.transaction.response_valid);
 printf("%d config-import lifecycle cases with real prepare/control/ACK pipeline passed; no hardware access\n",cases);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-config-apply-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_dla_command.c'),
            str(ROOT/'driver/n1x_dla_control.c'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    main()
