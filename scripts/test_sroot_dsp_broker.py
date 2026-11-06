#!/usr/bin/env python3
"""Exercise the actual DSP SROOT broker with fault-injected kernel edges."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    broker = source[source.index('static bool sroot_client_poisoned;'):
                    source.index('EXPORT_SYMBOL_GPL(n1x_sroot_upload_dsp_v1);')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "n1x_sroot.h"
#include "n1x_sroot_client.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
#define SHA256_DIGEST_SIZE 32
#define THIS_MODULE ((void *)1)
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
#define GFP_KERNEL 0
#define dev_info(...) ((void)0)
struct device { int unused; } dev;
struct n1x_boot_endpoint { struct device *dev; } endpoint = {&dev};
static struct n1x_boot_endpoint *boot_sroot = &endpoint, *boot_dla = &endpoint;
static bool boot_attempted = true, boot_stopping;
static int boot_error, boot_lock;
static struct { bool reply_valid; struct n1x_sroot_reply reply; } boot_result;
static int fault, exchanges, frees, pm_refs, module_refs, stage_calls, hash_index;
static bool valid_hash;
static void *allocation;
static const char *hashes[] = {
 "434660b36f4e21cb794bc50e10a47b91882710cbe90cb60620111a2a753c5b94",
 "350e0d40b2f8a6dfb486d8dfd3362fb5b02ceb90da372176b008498adbca8cc4",
 "517ef0d447ca80b7cb88ec0589cda816d9b20c844800ca95ccf7cbe96fc7d40e"};
static int hex2bin(u8 *out, const char *input, size_t count) {
 for(size_t i=0;i<count;i++){unsigned value;assert(sscanf(input+2*i,"%2x",&value)==1);out[i]=value;}
 return 0;
}
static void sha256(const u8 *data, size_t bytes, u8 *out) {
 assert(data && bytes);hex2bin(out,hashes[hash_index],32);if(!valid_hash)out[0]^=1;
}
static void mutex_lock(int *lock) { assert(!*lock);*lock=1; }
static void mutex_unlock(int *lock) { assert(*lock);*lock=0; }
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
 assert(boot_lock && d==&dev && flag==GFP_KERNEL && pm_refs==1 && !module_refs);
 if(fault==4)return NULL;allocation=calloc(1,bytes);assert(allocation);
 *dma=fault==5?0xe0000001:0xe0000000;return allocation;
}
static void dma_free_coherent(struct device *d,size_t bytes,void *cpu,dma_addr_t dma) {
 assert(boot_lock && d==&dev && bytes && cpu==allocation && dma && !module_refs && !exchanges);
 free(cpu);allocation=NULL;frees++;
}
static bool try_module_get(void *module) {
 assert(boot_lock && module==THIS_MODULE && pm_refs==1);
 if(fault==7)return false;module_refs++;return true;
}
static void dma_wmb(void) { assert(boot_lock && pm_refs==1 && module_refs==1); }
static int n1x_boot_write(void *ctx,u32 reg,u32 val) {(void)ctx;(void)reg;(void)val;assert(0);return -EIO;}
static int n1x_boot_read(void *ctx,u32 reg,u32 *val) {(void)ctx;(void)reg;(void)val;assert(0);return -EIO;}
static void n1x_boot_delay(void *ctx,u32 usec) {(void)ctx;(void)usec;assert(0);}
int n1x_sroot_stage_image(u8 *buf,size_t cap,const u8 *image,size_t bytes,
 __u64 dma,u32 minimum,u32 maximum,u32 words[3]) {
 assert(boot_lock && buf==image && buf==allocation && cap==bytes && bytes>4);
 assert(minimum==0xe0000000 && maximum==0xffffffff && dma==minimum);
 assert(!module_refs && !exchanges && pm_refs==1);stage_calls++;
 if(fault==6)return -ERANGE;
 words[0]=(u32)buf[0]|(u32)buf[1]<<8|(u32)buf[2]<<16|(u32)buf[3]<<24;
 words[1]=bytes;words[2]=dma;return 0;
}
int n1x_sroot_exchange(const struct n1x_dla_io *io,u32 stride,__u8 cmd,
 const u32 *words,__u8 count,struct n1x_sroot_reply *reply,u32 polls,u32 interval) {
 assert(boot_lock && module_refs==1 && pm_refs==1 && !frees && stage_calls==1);
 assert(io->context==boot_sroot && io->read32==n1x_boot_read && io->write32==n1x_boot_write);
 assert(stride==16 && cmd==0x18 && count==3 && polls==5001 && interval==1000);
 assert(words[0]==(u32)hash_index+0xc0303 && words[2]==0xe0000000);exchanges++;
 if(fault==8)return -ETIMEDOUT;
 *reply=(struct n1x_sroot_reply){.command=fault==10?0x19:0x18,.status=fault==9?1:0,.header=0x18};
 return 0;
}
''' + broker + r'''
static u8 image[631436];
static void reset(void) {
 free(allocation);allocation=NULL;memset(sroot_dsp_buffers,0,sizeof(sroot_dsp_buffers));
 sroot_client_poisoned=false;fault=exchanges=frees=pm_refs=module_refs=stage_calls=0;
 hash_index=1;valid_hash=true;boot_sroot=boot_dla=&endpoint;boot_attempted=true;boot_error=0;boot_stopping=false;
 boot_result.reply_valid=true;memset(&boot_result.reply,0,sizeof(boot_result.reply));
 assert(!boot_lock);
}
int main(void) {
 struct n1x_sroot_client_result result;
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
  if(guard==6)valid_hash=false;
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
 reset();puts("DSP SROOT broker guards, serialization and DMA retention passed");
}
'''
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'broker-test.c'
        binary = Path(temporary) / 'broker-test'
        source.write_text(harness)
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-g', '-I' + str(ROOT / 'include'), str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
