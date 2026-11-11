#!/usr/bin/env python3
"""Exercise the actual typed SROOT DSP REINIT with fault-injected PM edges."""
from pathlib import Path
from boot_test_context import context_layout
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = source[source.index('int n1x_sroot_reinit_dsp_v1('):
                      source.index('EXPORT_SYMBOL_GPL(n1x_sroot_reinit_dsp_v1);')]
    code = context_layout() + r'''
#include <stdlib.h>
#include "n1x_sroot.h"
#include "n1x_sroot_client.h"
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
struct device {int unused;};
static struct device consumer, supplier, root_device;
static struct n1x_boot_endpoint dla={.dev=&supplier}, sroot={.dev=&root_device};
static struct n1x_boot_context boot;
static struct n1x_sroot_client client;
static struct n1x_sroot_reinit_result result;
static int held, exchanges, fault;
static void mutex_lock(struct mutex *m){assert(m==&boot.lock && !held++);}
static void mutex_unlock(struct mutex *m){assert(m==&boot.lock && held--==1);}
static int n1x_boot_write(void *p,u32 a,u32 v){(void)p;(void)a;(void)v;assert(0);return -EIO;}
static int n1x_boot_read(void *p,u32 a,u32 *v){(void)p;(void)a;(void)v;assert(0);return -EIO;}
static void n1x_boot_delay(void *p,u32 us){(void)p;(void)us;assert(0);}
int n1x_sroot_exchange(const struct n1x_dla_io *io,u32 stride,u8 command,
 const u32 *words,u8 count,struct n1x_sroot_reply *reply,u32 polls,u32 interval)
{
 assert(held && io->context==&sroot && io->write32==n1x_boot_write && io->read32==n1x_boot_read);
 assert(stride==16 && command==0x1a && count==3 && polls==5001 && interval==1000);
 assert(words[0]==client.image_id && words[1]==0 && words[2]==1);
 assert(client.reinit_generation==boot.sleep_generation);exchanges++;
 if(fault==1)return -ETIMEDOUT;
 *reply=(struct n1x_sroot_reply){.header=0x1a,.command=0x1a};
 if(fault==2)reply->command=0x18;
 if(fault==3)reply->status=1;
 if(fault==4)reply->detail=1;
 if(fault==5)reply->count=1;
 if(fault==6)reply->header=0x11a;
 return 0;
}
''' + function + r'''
static void reset(u32 image)
{
 memset(&boot,0,sizeof(boot));memset(&client,0,sizeof(client));memset(&result,0xff,sizeof(result));
 held=exchanges=fault=0;boot.dla=&dla;boot.sroot=&sroot;boot.sleep_generation=1;
 boot.client_leases=boot.retention_leases=boot.ready_clients=boot.sleeping_clients=1;
 client=(struct n1x_sroot_client){.boot=&boot,.consumer=&consumer,.supplier=&supplier,
 .retention=true,.published=true,.reserved=true,.ready=true,.asleep=true,.image_id=image};
 boot.dsp_buffers[image-0xc0303]=(struct n1x_sroot_client_buffer){.published=true,.staging_released=true,.sleep_retained=true};
}
int main(void)
{
 reset(0xc0303);assert(n1x_sroot_reinit_dsp_v1(NULL,&result)==-EINVAL);
 assert(!result.attempted && !result.reply_valid && !result.reply_header);
 assert(n1x_sroot_reinit_dsp_v1(&client,NULL)==-EINVAL && !exchanges);
 for(unsigned g=0;g<18;g++) {
  reset(0xc0303);
  if(g==0)client.retention=false;if(g==1)client.published=false;if(g==2)client.ready=false;
  if(g==3)client.image_id=0xc0302;if(g==4)client.image_id=0xc0306;
  if(g==5)client.asleep=false;if(g==6)boot.sleep_generation=0;
  if(g==7)boot.removing=true;if(g==8)boot.dla=NULL;if(g==9)boot.sroot=NULL;
  if(g==10)client.supplier=&consumer;if(g==11)boot.suspended=true;
  if(g==12)boot.stopping=true;if(g==13)boot.loader_poisoned=true;if(g==14)boot.upload_error=-EIO;
  if(g==15)boot.dsp_buffers[0].published=false;if(g==16)boot.dsp_buffers[0].staging_released=false;
  if(g==17)boot.dsp_buffers[0].sleep_retained=false;
  assert(n1x_sroot_reinit_dsp_v1(&client,&result)<0 && !exchanges && !result.attempted && !held);
 }
 for(unsigned image=0xc0303;image<=0xc0305;image++)for(unsigned f=1;f<=6;f++) {
  reset(image);fault=f;assert(n1x_sroot_reinit_dsp_v1(&client,&result)<0);
  assert(exchanges==1 && result.attempted && !result.accepted && boot.loader_poisoned);
  assert(n1x_sroot_reinit_dsp_v1(&client,&result)<0 && exchanges==1);
 }
 for(unsigned image=0xc0303;image<=0xc0305;image++) {
  reset(image);
  for(unsigned cycle=1;cycle<=32;cycle++) {
   boot.sleep_generation=cycle;
   /* PM rollback has no sleep vote yet; it still belongs to this generation. */
   if(cycle&1){client.asleep=false;boot.sleep_prepared=true;}
   else {client.asleep=true;boot.sleep_prepared=false;}
   assert(!n1x_sroot_reinit_dsp_v1(&client,&result));
   assert(result.attempted && result.reply_valid && result.accepted && result.reply_header==0x1a);
   assert(!boot.loader_poisoned && exchanges==(int)cycle);
   assert(n1x_sroot_reinit_dsp_v1(&client,&result)==-EALREADY && exchanges==(int)cycle);
   assert(boot.client_leases==1 && boot.retention_leases==1 && boot.ready_clients==1 && boot.sleeping_clients==1);
  }
 }
 puts("SROOT DSP REINIT: bound image, native three-word packet, PM/rollback gates, one attempt per generation, terminal faults and 32 cycles: PASS");
}
'''
    with tempfile.TemporaryDirectory() as directory:
        c, binary = Path(directory)/'test.c', Path(directory)/'test'
        c.write_text(code)
        subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-misleading-indentation',
                        '-fsanitize=address,undefined','-g','-I'+str(ROOT/'include'),str(c),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True)

if __name__ == '__main__':
    main()
