#!/usr/bin/env python3
"""Exercise the actual DSP SROOT broker with fault-injected kernel edges."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    broker = source[source.index('static bool sroot_client_poisoned;'):
                    source.index('EXPORT_SYMBOL_GPL(n1x_sroot_upload_dsp_v2);')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <pthread.h>
#include "n1x_sroot.h"
#include "n1x_sroot_client.h"
#include "n1x_sroot_firmware.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
#define SHA256_DIGEST_SIZE 32
#define THIS_MODULE ((void *)1)
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
#define GFP_KERNEL 0
#define dev_info(...) ((void)0)
#define EXPORT_SYMBOL_GPL(name)
struct device { int unused; } dev;
struct n1x_boot_endpoint { struct device *dev; } endpoint = {&dev};
static struct n1x_boot_endpoint *boot_sroot = &endpoint, *boot_dla = &endpoint;
static bool boot_attempted = true, boot_stopping, boot_sleep_prepared;
static int boot_error, boot_lock;
static struct { bool reply_valid; struct n1x_sroot_reply reply; } boot_result;
static int fault, exchanges, frees, pm_refs, module_refs, stage_calls, hash_index;
static pthread_mutex_t boot_mutex=PTHREAD_MUTEX_INITIALIZER;
static _Thread_local unsigned lock_depth;
static bool allocation_published;
static unsigned leases(void);
static void *allocation;
static void mutex_lock(int *lock) {assert(lock==&boot_lock && !lock_depth);assert(!pthread_mutex_lock(&boot_mutex));assert(!*lock);*lock=1;lock_depth=1;}
static void mutex_unlock(int *lock) {assert(lock==&boot_lock && *lock && lock_depth);*lock=0;lock_depth=0;assert(!pthread_mutex_unlock(&boot_mutex));}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *e) {
 assert(boot_lock && e==boot_sroot);return fault==1?-EIO:0;
}
static int dma_set_mask_and_coherent(struct device *d, uint64_t mask) {
 assert(boot_lock && d==&dev && mask==0xffffffff);return fault==2?-EIO:0;
}
static int pm_runtime_resume_and_get(struct device *d) {
 assert(boot_lock && d==&dev);if(fault==3)return -EIO;pm_refs++;return 0;
}
static void pm_runtime_put(struct device *d) { assert(boot_lock && d==&dev && pm_refs>0);pm_refs--; }
static void *dma_alloc_coherent(struct device *d,size_t bytes,dma_addr_t *dma,int flag) {
 assert(boot_lock && d==&dev && flag==GFP_KERNEL && pm_refs==(int)leases()+1 && module_refs==(int)leases());
 if(fault==4)return NULL;allocation=calloc(1,bytes);assert(allocation);allocation_published=false;
 *dma=fault==5?0xe0000001:0xe0000000;return allocation;
}
static void dma_free_coherent(struct device *d,size_t bytes,void *cpu,dma_addr_t dma) {
 assert(boot_lock && d==&dev && bytes && cpu==allocation && dma);
 if(allocation_published)assert(module_refs==(int)leases() && pm_refs==module_refs && !fault);
 else assert(module_refs==(int)leases() && pm_refs==module_refs+1);
 free(cpu);allocation=NULL;frees++;
}
static bool try_module_get(void *module) {
 assert(boot_lock && module==THIS_MODULE && pm_refs==(int)leases()+1 && module_refs==(int)leases());
 if(fault==7)return false;module_refs++;return true;
}
static void dma_wmb(void) { assert(boot_lock && pm_refs==module_refs && module_refs==(int)leases()+1); }
static int n1x_boot_write(void *ctx,u32 reg,u32 val) {(void)ctx;(void)reg;(void)val;assert(0);return -EIO;}
static int n1x_boot_read(void *ctx,u32 reg,u32 *val) {(void)ctx;(void)reg;(void)val;assert(0);return -EIO;}
static void n1x_boot_delay(void *ctx,u32 usec) {(void)ctx;(void)usec;assert(0);}
int n1x_sroot_stage_image(u8 *buf,size_t cap,const u8 *image,size_t bytes,
 __u64 dma,u32 minimum,u32 maximum,u32 words[3]) {
 assert(boot_lock && buf==image && buf==allocation && cap==bytes && bytes>4);
 assert(minimum==0xe0000000 && maximum==0xffffffff && dma==minimum);
 assert(module_refs==(int)leases() && pm_refs==module_refs+1);stage_calls++;
 if(fault==6)return -ERANGE;
 words[0]=(u32)buf[0]|(u32)buf[1]<<8|(u32)buf[2]<<16|(u32)buf[3]<<24;
 words[1]=bytes;words[2]=dma;return 0;
}
int n1x_sroot_exchange(const struct n1x_dla_io *io,u32 stride,__u8 cmd,
 const u32 *words,__u8 count,struct n1x_sroot_reply *reply,u32 polls,u32 interval) {
 assert(boot_lock && module_refs==(int)leases() && pm_refs==module_refs && stage_calls==exchanges+1);
 allocation_published=true;
 assert(io->context==boot_sroot && io->read32==n1x_boot_read && io->write32==n1x_boot_write);
 assert(stride==16 && cmd==0x18 && count==3 && polls==5001 && interval==1000);
 assert(words[0]==(u32)hash_index+0xc0303 && words[2]==0xe0000000);exchanges++;
 if(fault==8)return -ETIMEDOUT;
 *reply=(struct n1x_sroot_reply){.command=fault==10?0x19:0x18,.status=fault==9?1:0,.header=fault==11?0x118:fault==12?0x1000018:0x18,.count=fault==11?1:0};
 return 0;
}
''' + broker + r'''
static unsigned leases(void) {unsigned count=0;for(unsigned i=0;i<3;i++)count+=sroot_dsp_buffers[i].published;return count;}
static u8 image[631436];
static void image_put32(u8 *p,u32 value) {for(unsigned i=0;i<4;i++)p[i]=value>>(8*i);}
static void make_image(u8 *p,size_t bytes,u32 id)
{
 assert(bytes>=83);memset(p,0,bytes);p[0]=0x59;
 u8 *n=p+3;image_put32(n,1);image_put32(n+4,2);image_put32(n+8,0xefbeadde);
 image_put32(n+12,0xefbeadde);image_put32(n+16,id);image_put32(n+32,80);
 image_put32(n+44,0xefbeadde);image_put32(n+48,id);image_put32(n+56,80);
 image_put32(n+64,bytes-83);image_put32(n+76,0xefbeadde);
}
static void reset(void) {
 free(allocation);allocation=NULL;memset(sroot_dsp_buffers,0,sizeof(sroot_dsp_buffers));
 sroot_client_poisoned=false;allocation_published=false;fault=exchanges=frees=pm_refs=module_refs=stage_calls=0;
 hash_index=1;make_image(image,sizeof(image),0xc0304);boot_sroot=boot_dla=&endpoint;boot_attempted=true;boot_error=0;boot_stopping=boot_sleep_prepared=false;
 boot_result.reply_valid=true;memset(&boot_result.reply,0,sizeof(boot_result.reply));
 assert(!boot_lock);
}
struct thread_result {int error;struct n1x_sroot_client_result_v2 result;};
static void *upload_thread(void *ptr) {struct thread_result *r=ptr;r->error=n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&r->result);return NULL;}
int main(void) {
 struct n1x_sroot_client_result result;
 struct n1x_sroot_client_result_v2 result2;
 reset();boot_sleep_prepared=true;
 assert(n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&result2)==-EBUSY);
 assert(!result2.published && !pm_refs && !module_refs && !allocation && !exchanges);
 for(int failure=1;failure<=10;failure++) {
  reset();fault=failure;
  int err=n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result);assert(err<0);
  assert(!boot_lock);
  if(failure<=7) {
   assert(!result.published && !exchanges && !pm_refs && !module_refs && !allocation);
   assert(!sroot_client_poisoned);
  }else{
   assert(result.published && !result.accepted && pm_refs==1 && module_refs==1 && allocation && !frees);
   assert(sroot_client_poisoned);
   assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result)==-ENOTRECOVERABLE);
   assert(exchanges==1);
  }
 }
 reset();assert(!n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result));
 assert(result.published && result.reply_valid && result.accepted && module_refs==1 && pm_refs==1);
 assert(!sroot_client_poisoned && !frees);
 assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result)==-EALREADY && exchanges==1);
 for(int guard=0;guard<8;guard++) {
  reset();
  if(guard==0)boot_sroot=NULL;
  if(guard==1)boot_dla=NULL;
  if(guard==2)boot_attempted=false;
  if(guard==3)boot_error=-EIO;
  if(guard==4)boot_result.reply_valid=false;
  if(guard==5)boot_result.reply.status=1;
  if(guard==6)image[0]=0;
  if(guard==7)boot_stopping=true;
  assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result)<0);
  assert(!result.published && !pm_refs && !module_refs && !allocation && !exchanges);
 }
 reset();boot_stopping=true;
 assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result)==-ESHUTDOWN);
 assert(!result.published && !exchanges && !allocation && !pm_refs && !module_refs);
 reset();assert(n1x_sroot_upload_dsp_v1(0xc0302,image,sizeof(image),&result)==-EINVAL);
 assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image)-1,&result)==-EBADMSG);
 assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),NULL)==-EINVAL);

 for(int failure=1;failure<=12;failure++){
  reset();fault=failure;int error=n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&result2);assert(error<0);
  assert(!result2.staging_released && !sroot_dsp_buffers[1].staging_released);
  if(failure<=7)assert(!result2.published && !exchanges && !pm_refs && !module_refs && !allocation);
  else {
   assert(result2.published && !result2.accepted && pm_refs==1 && module_refs==1 && allocation && !frees);
   assert(sroot_client_poisoned && sroot_dsp_buffers[1].published);
   assert(n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&result2)==-ENOTRECOVERABLE && exchanges==1);
  }
 }
 reset();assert(!n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&result2));
 assert(result2.published && result2.reply_valid && result2.accepted && result2.staging_released);
 assert(module_refs==1 && pm_refs==1 && frees==1 && !allocation && !sroot_client_poisoned);
 assert(sroot_dsp_buffers[1].published && sroot_dsp_buffers[1].staging_released);
 assert(!sroot_dsp_buffers[1].cpu && !sroot_dsp_buffers[1].dma && sroot_dsp_buffers[1].bytes==sizeof(image)+4);
 assert(n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result)==-EALREADY && exchanges==1 && frees==1);
 reset();assert(!n1x_sroot_upload_dsp_v1(0xc0304,image,sizeof(image),&result));
 assert(n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),&result2)==-EALREADY && allocation && !frees);

 reset();const unsigned order[]={1,2,0};const size_t sizes[]={363816,631436,157996};
 for(unsigned i=0;i<3;i++){
  hash_index=order[i];make_image(image,sizes[hash_index],0xc0303+hash_index);assert(!n1x_sroot_upload_dsp_v2(0xc0303+hash_index,image,sizes[hash_index],&result2));
  assert(result2.accepted && result2.staging_released && !allocation);
  assert(pm_refs==(int)i+1 && module_refs==(int)i+1 && frees==(int)i+1 && leases()==i+1);
 }
 for(unsigned i=0;i<3;i++)assert(sroot_dsp_buffers[i].published && sroot_dsp_buffers[i].staging_released && !sroot_dsp_buffers[i].cpu && !sroot_dsp_buffers[i].dma);
 reset();assert(n1x_sroot_upload_dsp_v2(0xc0304,image,sizeof(image),NULL)==-EINVAL && !exchanges && !allocation);
 reset();pthread_t threads[8];struct thread_result results[8]={0};
 for(unsigned i=0;i<8;i++)assert(!pthread_create(&threads[i],NULL,upload_thread,&results[i]));
 unsigned successful=0;
 for(unsigned i=0;i<8;i++){
  assert(!pthread_join(threads[i],NULL));
  if(!results[i].error){successful++;assert(results[i].result.staging_released && results[i].result.accepted);}
  else assert(results[i].error==-EALREADY && !results[i].result.published && !results[i].result.staging_released);
 }
 assert(successful==1 && exchanges==1 && frees==1 && !allocation && module_refs==1 && pm_refs==1);
 assert(sroot_dsp_buffers[1].published && sroot_dsp_buffers[1].staging_released && !boot_lock);
 reset();puts("DSP SROOT broker v1 retention/v2 staging release, terminal faults and eight concurrent callers passed");
}
'''
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'broker-test.c'
        binary = Path(temporary) / 'broker-test'
        source.write_text(harness)
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-pthread', '-g', '-I' + str(ROOT / 'include'), str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
