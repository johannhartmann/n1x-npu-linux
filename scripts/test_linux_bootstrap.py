#!/usr/bin/env python3
"""Fault-check actual Linux upload/firmware validation code with mocked OS edges.

Requires OpenSSL development headers for the userspace SHA-256 test shim.
No module is loaded and no hardware is accessed.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    validator = source[source.index('static int n1x_boot_validate_firmware('):
                       source.index('/* Caller holds boot_lock.')]
    upload = source[source.index('static int n1x_boot_upload('):
                    source.index('/* Caller holds boot_lock. The upload already')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <openssl/sha.h>
#include "n1x_dla_boot.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
#define N1X_BOOT_BYTES 67508
#define N1X_BOOT_FIRMWARE "nvidia/n1x/p16-sroot-v0.bin"
#define SHA256_DIGEST_SIZE 32
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0
#define THIS_MODULE ((void *)0x1234)
#define dev_info(...) ((void)0)
struct device {int id;};
struct firmware {size_t size;const u8 *data;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device root_dev={0},dla_dev={1};
static struct n1x_boot_endpoint root_ep={&root_dev},dla_ep={&dla_dev};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static bool enable_upload,machine_ok,boot_attempted;
static struct n1x_dla_sroot_load_result boot_result;
static void *boot_buffer;
static dma_addr_t boot_dma,allocation_address;
static u8 image[N1X_BOOT_BYTES],storage[N1X_BOOT_BYTES];
static struct firmware firmware;
static int gate_error[2],request_error,mask_error,resume_error[2];
static int resume_refs[2],allocations,frees,releases,module_refs,uploads,barriers,cases;
static int upload_error;
static bool allocate_fail,module_get_fail;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {return gate_error[ep->dev->id];}
static int request_firmware_direct(const struct firmware **out,const char *name,struct device *dev)
{assert(dev==&root_dev && !strcmp(name,N1X_BOOT_FIRMWARE));
 if(request_error)return request_error;*out=&firmware;return 0;}
static void release_firmware(const struct firmware *fw) {assert(fw==&firmware);releases++;}
static void sha256(const u8 *data,size_t bytes,u8 digest[32])
{assert(SHA256(data,bytes,digest)==digest);}
static int dma_set_mask_and_coherent(struct device *dev,uint64_t mask)
{assert(dev==&root_dev && mask==0xffffffffULL);return mask_error;}
static int pm_runtime_resume_and_get(struct device *dev)
{if(resume_error[dev->id])return resume_error[dev->id];resume_refs[dev->id]++;return 0;}
static void pm_runtime_put(struct device *dev)
{assert(resume_refs[dev->id]>0);resume_refs[dev->id]--;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *address,int flags)
{assert(dev==&root_dev && bytes==N1X_BOOT_BYTES && flags==GFP_KERNEL);
 allocations++;*address=allocation_address;memset(storage,0xa5,sizeof(storage));
 return allocate_fail?NULL:storage;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t address)
{assert(dev==&root_dev && bytes==N1X_BOOT_BYTES && buffer==storage && address==allocation_address);
 assert(!boot_attempted && !module_refs);frees++;}
static bool try_module_get(void *module)
{assert(module==THIS_MODULE);if(module_get_fail)return false;module_refs++;return true;}
static int n1x_boot_read(void *ctx,u32 offset,u32 *value)
{(void)ctx;(void)offset;(void)value;abort();}
static int n1x_boot_write(void *ctx,u32 offset,u32 value)
{(void)ctx;(void)offset;(void)value;abort();}
static void n1x_boot_delay(void *ctx,u32 interval) {(void)ctx;(void)interval;abort();}
static void dma_wmb(void) {barriers++;}
int n1x_dla_sroot_upload(const struct n1x_dla_io *root,const struct n1x_dla_io *dla,
                        u32 stride,const u32 words[3],u32 polls,u32 interval,
                        struct n1x_dla_sroot_load_result *result)
{assert(boot_attempted && module_refs==1 && resume_refs[0]==1 && resume_refs[1]==1 && barriers==1);
 assert(root->context==&root_ep && dla->context==&dla_ep && !dla->write32);
 assert(root->write32==n1x_boot_write && root->read32==n1x_boot_read);
 assert(stride==16 && polls==5001 && interval==1000);
 assert(words[0]==0xc0302 && words[1]==N1X_BOOT_BYTES && words[2]==allocation_address);
 assert(!memcmp(storage,image+4,N1X_BOOT_BYTES-4));
 assert(!memcmp(storage+N1X_BOOT_BYTES-4,"\0\0\0\0",4));
 uploads++;result->upload_attempted=1;result->reply_valid=upload_error==0;
 result->idle_observed=upload_error==0;return upload_error;}
'''
    harness += validator + upload
    harness += r'''
static void reset(void)
{boot_sroot=&root_ep;boot_dla=&dla_ep;enable_upload=machine_ok=true;boot_attempted=false;
 memset(&boot_result,0,sizeof(boot_result));boot_buffer=NULL;boot_dma=0;
 allocation_address=0xe0010000ULL;gate_error[0]=gate_error[1]=0;
 request_error=mask_error=resume_error[0]=resume_error[1]=0;
 resume_refs[0]=resume_refs[1]=0;allocations=frees=releases=module_refs=uploads=barriers=0;
 allocate_fail=module_get_fail=false;upload_error=0;firmware=(struct firmware){sizeof(image),image};}
static void check(int expected,int allocs,int free_count,int release_count,bool attempted)
{assert(n1x_boot_upload()==expected);assert(allocations==allocs && frees==free_count && releases==release_count);
 assert(boot_attempted==attempted);assert(module_refs==(int)attempted);
 assert(resume_refs[0]==(int)attempted && resume_refs[1]==(int)attempted);
 assert(uploads==(int)attempted && barriers==(int)attempted);
 assert(!!boot_buffer==(attempted && !allocate_fail));cases++;}
int main(int argc,char **argv)
{assert(argc==2);FILE *file=fopen(argv[1],"rb");assert(file);
 assert(fread(image,1,sizeof(image),file)==sizeof(image));assert(fgetc(file)==EOF);fclose(file);
 reset();assert(n1x_boot_validate_firmware(&firmware)==0);
 image[123]^=1;assert(n1x_boot_validate_firmware(&firmware)==-EBADMSG);image[123]^=1;
 firmware.size--;assert(n1x_boot_validate_firmware(&firmware)==-EBADMSG);
 assert(n1x_boot_validate_firmware(NULL)==-EBADMSG);
 reset();enable_upload=false;check(-EPERM,0,0,0,false);
 reset();machine_ok=false;check(-EPERM,0,0,0,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,0,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,0,false);
 reset();gate_error[0]=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,0,false);
 reset();gate_error[1]=-EOPNOTSUPP;check(-EOPNOTSUPP,0,0,0,false);
 reset();request_error=-ENOENT;check(-ENOENT,0,0,0,false);
 reset();firmware.size--;check(-EBADMSG,0,0,1,false);
 reset();image[123]^=1;check(-EBADMSG,0,0,1,false);image[123]^=1;
 reset();mask_error=-EIO;check(-EIO,0,0,1,false);
 reset();resume_error[0]=-EIO;check(-EIO,0,0,1,false);
 reset();resume_error[1]=-EIO;check(-EIO,0,0,1,false);
 reset();allocate_fail=true;check(-ENOMEM,1,0,1,false);
 reset();allocation_address=0xe0010001ULL;check(-ERANGE,1,1,1,false);
 reset();allocation_address=0x1000;check(-EINVAL,1,1,1,false);
 reset();allocation_address=0xfffff000ULL;check(-EINVAL,1,1,1,false);
 reset();module_get_fail=true;check(-ENODEV,1,1,1,false);
 reset();check(0,1,0,1,true);
 assert(n1x_boot_upload()==-EALREADY);assert(allocations==1 && uploads==1 && module_refs==1);
 reset();upload_error=-ETIMEDOUT;check(-ETIMEDOUT,1,0,1,true);
 assert(n1x_boot_upload()==-EALREADY);assert(allocations==1 && uploads==1 && module_refs==1);
 reset();upload_error=-EREMOTEIO;check(-EREMOTEIO,1,0,1,true);
 reset();upload_error=-EIO;check(-EIO,1,0,1,true);
 printf("%d upload lifecycle cases; real vendor SHA-256 and corruption/size controls passed; no hardware access\n",cases);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-linux-bootstrap-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_sroot.c'),
            '-lcrypto', '-o', str(executable)], check=True)
        subprocess.run([str(executable), str(ROOT/'sources/dla-p16-sroot-v0.bin')], check=True)


if __name__ == '__main__':
    main()
