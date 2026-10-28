#!/usr/bin/env python3
"""Compile actual accel ioctl/lifetime code with modeled Linux user/DMA boundaries.

No device is opened. The execution backend is a stub; protocol and firmware are
covered independently by test_linux_bypass_task/firmware.py.
"""
import argparse
import os
import hashlib
import struct
import sys
from pathlib import Path
import shlex
import subprocess
import tempfile
from emulate_profile1_commands import run, HOST, MMIO, ACK, PROFILE1_HASH

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--conv', action='store_true', help='also qualify convolution task serialization/scheduling')
    parser.add_argument('--conv-relu', action='store_true', help='also qualify Conv->ReLU task serialization/scheduling')
    args = parser.parse_args()
    source = (ROOT / 'driver/n1x_dla_accel.c').read_text()
    body = source[source.index('#define N1X_JOB_PREFIX'):source.index('/* No GEM mmap:')]
    shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_job.h"
#include "n1x_dla_task.h"
#include "n1x_dla_accel.h"
#include "uapi/n1x_dla_accel.h"
typedef __u8 u8;typedef __u32 u32;typedef __u64 u64;typedef __u32 __le32;
#define U64_MAX UINT64_MAX
#define CAP_SYS_RAWIO 17
#define GFP_KERNEL 0
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
#define PAGE_ALIGN(x) (((x)+4095UL)&~4095UL)
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
#define u64_to_user_ptr(x) ((void *)(uintptr_t)(x))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define ERR_PTR(x) ((void *)(intptr_t)(x))
#define PTR_ERR(x) ((int)(intptr_t)(x))
#define IS_ERR(x) ((uintptr_t)(x)>=(uintptr_t)-4095)
struct device {int unused;};
struct drm_device {struct device *dev;};
struct drm_file {int unused;};
struct mutex {bool locked;};
struct list_head {struct list_head *next,*prev;};
static void INIT_LIST_HEAD(struct list_head *h) {h->next=h->prev=h;}
static void list_add_tail(struct list_head *n,struct list_head *h)
{n->next=h;n->prev=h->prev;h->prev->next=n;h->prev=n;}
#define list_for_each_entry(pos,head,member) \
 for(struct list_head *node=(head)->next;node!=(head) && ((pos)=container_of(node,__typeof__(*(pos)),member),1);node=node->next)
static bool privileged=true,copyin_fail,copyout_fail,memdup_fail,dma_fail;
static int heap_fail_at=-1,heap_calls,allocations,frees,submits,backend_error;
static bool backend_attempt=true;
static u64 dma_address=0x100000000ULL;
static unsigned char image[32U << 20];
static size_t image_bytes=4096;
static const char *capture_file;
static void mutex_lock(struct mutex *m) {assert(!m->locked);m->locked=true;}
static void mutex_unlock(struct mutex *m) {assert(m->locked);m->locked=false;}
static bool capable(int cap) {assert(cap==CAP_SYS_RAWIO);return privileged;}
static void *kcalloc(size_t n,size_t size,int flags)
{assert(!flags);return heap_calls++==heap_fail_at?NULL:calloc(n,size);}
static void *kzalloc(size_t size,int flags) {return kcalloc(1,size,flags);}
static void kfree(void *p) {free(p);}
static void *memdup_user(const void *p,size_t size)
{if(memdup_fail)return ERR_PTR(-EFAULT);void *out=malloc(size);assert(out);memcpy(out,p,size);return out;}
static int copy_from_user(void *out,const void *in,size_t n)
{if(copyin_fail)return 1;memcpy(out,in,n);return 0;}
static int copy_to_user(void *out,const void *in,size_t n)
{if(copyout_fail)return 1;memcpy(out,in,n);return 0;}
static void *dma_alloc_coherent(struct device *d,size_t n,u64 *dma,int flags)
{assert(d&&!flags);allocations++;*dma=dma_address;return dma_fail?NULL:malloc(n);}
static void dma_free_coherent(struct device *d,size_t n,void *p,u64 dma)
{assert(d&&n&&p&&dma==dma_address);frees++;free(p);}
static void dma_wmb(void) {}
static void dma_rmb(void) {}
'''
    tests = r'''
static struct device device;
static struct drm_file owner,other;
static struct n1x_dla_accel accel;
static struct n1x_dla_accel_range ranges[512];
static struct n1x_dla_accel_exec req;
static int cases;
static u64 word64(const u8 *p) {u64 v=0;for(int i=0;i<8;i++)v|=(u64)p[i]<<(8*i);return v;}
static int backend(struct device *d,struct n1x_dla_job *task,const struct n1x_dla_job_ops *ops)
{
 assert(d==&device);submits++;
 if(!backend_attempt)return backend_error;
 u8 *p=task->buffer;
 assert(word64(p+21)==task->dma+N1X_JOB_ADDRESSES);
 assert(word64(p+65)==task->dma+N1X_JOB_FENCE);
 if(capture_file){
  assert(!memcmp(p+N1X_JOB_PREFIX,image,image_bytes));
  assert(!ops->publish(ops->context,19));
  FILE *out=fopen(capture_file,"wb");assert(out);
  size_t n=PAGE_ALIGN(image_bytes+N1X_JOB_PREFIX);
  assert(fwrite(p,1,n,out)==n&&!fclose(out));
  return -ECANCELED; /* Captured before hardware publication; may free. */
 }
 assert(word64(p+N1X_JOB_ADDRESSES)==task->dma+N1X_JOB_PREFIX);
 assert(word64(p+N1X_JOB_ADDRESSES+8)==task->dma+N1X_JOB_PREFIX+128);
 assert(!memcmp(p+N1X_JOB_PREFIX,image,image_bytes));
 assert(p[41]==0 && p[29]==2); /* arithmetic task, two local addresses */
 assert(!ops->publish(ops->context,19));
 u32 value;assert(!ops->read_fence(ops->context,&value)&&value==~19U);
 assert(p[73]==19);
 task->attempted=true;
 if(backend_error)return backend_error;
 task->verified=true;
 p[N1X_JOB_PREFIX+13]=0x5a;
 return 0;
}
static void reset_request(void)
{req=(struct n1x_dla_accel_exec){.image_ptr=(uintptr_t)image,.ranges_ptr=(uintptr_t)ranges,
 .image_bytes=image_bytes,.num_addresses=2};
 ranges[0]=(struct n1x_dla_accel_range){0,128};ranges[1]=(struct n1x_dla_accel_range){128,128};
 privileged=true;copyin_fail=copyout_fail=memdup_fail=dma_fail=false;
 heap_fail_at=-1;heap_calls=0;backend_error=0;backend_attempt=true;dma_address=0x100000000ULL;}
static void check(int expected)
{int oldalloc=allocations,oldfree=frees;u32 count=accel.count;
 int ret=exec_ioctl(&accel.drm,&req,&owner);assert(ret==expected);
 assert(!accel.lock.locked);
 if(expected){assert(!req.job_id&&accel.count==count);
  assert(frees-oldfree==allocations-oldalloc-(dma_fail?1:0));}
 cases++;}
int main(int argc,char **argv)
{
 accel.drm.dev=&device;accel.submit=backend;INIT_LIST_HEAD(&accel.jobs);memset(image,0xab,image_bytes);
 if(argc==4){
  FILE *in=fopen(argv[1],"rb");assert(in&&!fseek(in,0,SEEK_END));long n=ftell(in);
  assert(n>0&&(size_t)n<=sizeof(image)&&!fseek(in,0,SEEK_SET));image_bytes=n;
  assert(fread(image,1,image_bytes,in)==image_bytes&&fgetc(in)==EOF&&!fclose(in));
  capture_file=argv[3];reset_request();dma_address=CAPTURE_DMA;
  in=fopen(argv[2],"rb");assert(in);
  assert(!fseek(in,0,SEEK_END));n=ftell(in);
  assert(n>0&&(size_t)n<=sizeof(ranges)&&n%sizeof(ranges[0])==0&&!fseek(in,0,SEEK_SET));
  req.num_addresses=n/sizeof(ranges[0]);
  assert(fread(ranges,1,n,in)==(size_t)n&&fgetc(in)==EOF&&!fclose(in));
  check(-ECANCELED);return 0;
 }
 assert(argc==1);
 reset_request();privileged=false;check(-EPERM);assert(!allocations&&!submits);
 struct n1x_dla_accel_info info;
 assert(info_ioctl(&accel.drm,&info,&owner)==-EPERM);
 struct n1x_dla_accel_read read={0};
 assert(read_ioctl(&accel.drm,&read,&owner)==-EPERM);
 for(int mode=0;mode<10;mode++){
  reset_request();
  switch(mode){case 0:req.flags=1;break;case 1:req.reserved=1;break;case 2:req.job_id=1;break;
  case 3:req.status=-EIO;break;case 4:req.completed=1;break;case 5:req.image_ptr=0;break;
  case 6:req.ranges_ptr=0;break;case 7:req.image_bytes=0;break;
  case 8:req.image_bytes=N1X_DLA_MAX_IMAGE+1;break;case 9:req.num_addresses=513;break;}
  /* Invalid caller output fields are unchanged on rejected ioctls. */
  assert(exec_ioctl(&accel.drm,&req,&owner)==-EINVAL);cases++;
 }
 reset_request();req.num_addresses=0;check(-EINVAL);
 reset_request();ranges[1].offset=UINT64_MAX;check(-EINVAL);
 reset_request();ranges[1].bytes=UINT64_MAX;check(-EINVAL);
 reset_request();ranges[1].offset=image_bytes;check(-EINVAL);
 reset_request();ranges[1].bytes=0;check(-EINVAL);
 reset_request();memdup_fail=true;check(-EFAULT);
 reset_request();heap_fail_at=0;check(-ENOMEM);
 reset_request();heap_fail_at=1;check(-ENOMEM);
 reset_request();accel.unplugged=true;check(-ENODEV);accel.unplugged=false;
 reset_request();accel.count=N1X_DLA_MAX_JOBS;check(-ENOSPC);accel.count=0;
 reset_request();accel.retained=N1X_DLA_RETAIN_LIMIT;check(-ENOSPC);accel.retained=0;
 reset_request();accel.last_id=U64_MAX;check(-EOVERFLOW);accel.last_id=0;
 reset_request();dma_fail=true;check(-ENOMEM);
 for(int mode=0;mode<3;mode++){reset_request();dma_address=mode==0?0:mode==1?1:DMA_BIT_MASK(36)-4095;check(-ERANGE);}
 reset_request();copyin_fail=true;check(-EFAULT);
 reset_request();backend_attempt=false;backend_error=-ENODEV;check(-ENODEV);
 reset_request();backend_attempt=false;check(-EIO);
 reset_request();check(0);assert(req.job_id==1&&req.completed&&req.status==0);
 assert(accel.count==1&&accel.retained==12288);
 unsigned char output=0;
 read=(struct n1x_dla_accel_read){.job_id=req.job_id,.offset=13,.bytes=1,.data_ptr=(uintptr_t)&output};
 assert(!read_ioctl(&accel.drm,&read,&owner)&&output==0x5a);
 assert(read_ioctl(&accel.drm,&read,&other)==-ENOENT);
 copyout_fail=true;assert(read_ioctl(&accel.drm,&read,&owner)==-EFAULT);copyout_fail=false;
 read.offset=image_bytes;assert(read_ioctl(&accel.drm,&read,&owner)==-EINVAL);
 read.offset=0;read.bytes=UINT64_MAX;assert(read_ioctl(&accel.drm,&read,&owner)==-EINVAL);
 read.bytes=1;read.reserved=1;assert(read_ioctl(&accel.drm,&read,&owner)==-EINVAL);read.reserved=0;
 int savedfrees=frees;postclose(&accel.drm,&owner);
 assert(frees==savedfrees&&accel.count==1&&read_ioctl(&accel.drm,&read,&owner)==-ENOENT);
 reset_request();backend_error=-ETIMEDOUT;check(0);
 assert(req.job_id==2&&!req.completed&&req.status==-ETIMEDOUT&&frees==savedfrees);
 read.job_id=2;assert(read_ioctl(&accel.drm,&read,&owner)==-EIO);
 assert(!info_ioctl(&accel.drm,&info,&owner)&&info.retained_jobs==2&&info.retained_bytes==24576);
 assert(info.abi_version==1&&info.capabilities==3&&info.reserved==0);
 /* Only the test may discard modeled device storage; production retains it. */
 while(accel.jobs.next!=&accel.jobs){struct n1x_accel_job *j=container_of(accel.jobs.next,struct n1x_accel_job,link);
  accel.jobs.next=j->link.next;free(j->task.buffer);free(j);}
 printf("%d exact accel EXEC cases plus READ/INFO/ownership/close checks passed; no hardware access\n",cases);
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-accel-') as directory:
        code, binary = Path(directory)/'test.c', Path(directory)/'test'
        code.write_text('#define CAPTURE_DMA '+hex(HOST)+'ULL\n'+shim + body + tests)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_dla_task.c'),
            '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        sys.path.insert(0, str(ROOT/'runtime'))
        from run_relu import prepare
        template = (ROOT/'results/p16-relu-template.bin').read_bytes()
        input_bytes = struct.pack('<1568e', *[1.5 if i % 2 else -2.5 for i in range(1568)])
        image, ranges, expected = prepare(template, input_bytes)
        assert expected == struct.pack('<1568e', *[1.5 if i % 2 else 0 for i in range(1568)])
        from loadable import Model
        model = Model.from_file(ROOT/'results/native-compile-t254/relu.nvda')
        native_image, native_ranges = model.prepare({'input': input_bytes})
        fixtures = [
                ('template', image, ranges, 0),
                ('native_loadable', native_image, native_ranges, model.inputs[0].offset)]
        if args.conv:
            from verify_conv_loadable import LOADABLE_HASH as CONV_HASH
            conv = Model.from_file(ROOT/'results/native-compile-t254-conv1x1/conv1x1.nvda')
            assert conv.sha256 == CONV_HASH
            conv_image, conv_ranges = conv.prepare({conv.inputs[0].name: input_bytes})
            fixtures.append(('conv_loadable', conv_image, conv_ranges, conv.inputs[0].offset))
        if args.conv_relu:
            from verify_conv_loadable import CONV_RELU_HASH
            combined=Model.from_file(ROOT/'results/native-compile-t254-conv1x1_relu/conv1x1_relu.nvda')
            assert combined.sha256==CONV_RELU_HASH
            combined_image,combined_ranges=combined.prepare({combined.inputs[0].name:input_bytes})
            fixtures.append(('conv_relu_loadable',combined_image,combined_ranges,combined.inputs[0].offset))
        cases = []
        for label, source_image, source_ranges, input_offset in fixtures:
            payload_path, captured = Path(directory)/'image.bin', Path(directory)/'prepared.bin'
            range_path = Path(directory)/'ranges.bin'
            payload_path.write_bytes(source_image)
            range_path.write_bytes(b''.join(struct.pack('<QQ', *r) for r in source_ranges))
            subprocess.run([str(binary), str(payload_path), str(range_path), str(captured)], check=True)
            payload = bytearray(captured.read_bytes())
            assert payload[8192+input_offset:8192+input_offset+3136] == input_bytes
            if label == 'template':
                assert payload[12288:12288+3136] == b'\xa5'*3136
            assert [struct.unpack_from('<Q', payload, 512+8*i)[0] for i in range(len(source_ranges))] == [HOST+8192+offset for offset, _ in source_ranges]
            cases.append((label, payload, source_ranges))
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    def response(u):
        for offset in (8, 0xd28):
            value = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
            u.mem_write(MMIO+offset, struct.pack('<I', value & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))
    for label, payload, ranges in cases:
        result = run(firmware, method=0x307, host_payload=payload, host_handler=response,
                     boot_from_reset=True, via_trap=True, dma_register_model=True, resume_scheduler=True)
        assert result['returned'] and result['scheduler_wfi']
        assert not result['function_stubs'] and result['firmware_code_unmodified']
        assert result['interrupts'] == [(0x702, HOST >> 8)] and not result['task_interrupts']
        for i, (offset, _) in enumerate(ranges[:-1]):
            assert (0x2505c+8*i, 4, HOST+8192+offset) in result['scheduler_mmio_writes']
            assert (0x25060+8*i, 4, 0) in result['scheduler_mmio_writes']
        print(label + ': runtime image -> actual accel ioctl serializer -> original firmware scheduler passed; engine arithmetic unmodeled')


if __name__ == '__main__':
    main()
