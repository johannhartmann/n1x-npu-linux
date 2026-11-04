#!/usr/bin/env python3
"""Compile actual accel ioctl/lifetime code with modeled Linux user/DMA boundaries.

No device is opened. The execution backend is a stub; protocol and firmware are
covered independently by test_linux_bypass_task/firmware.py.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

HOST = 0x80000000
ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_dla_accel.c').read_text()
    body = source[source.index('#define N1X_JOB_PREFIX'):source.index('static int n1x_accel_open(')]
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
#include "n1x_dla_command.h"
#include "n1x_dla_statistics.h"
#include "n1x_dla_task.h"
#include "n1x_dla_accel.h"
#include "uapi/n1x_dla_accel.h"
typedef __u8 u8;typedef __u32 u32;typedef __u64 u64;typedef __u32 __le32;typedef __u64 __le64;
#define U32_MAX UINT32_MAX
#define U64_MAX UINT64_MAX
#define CAP_SYS_RAWIO 17
#define GFP_KERNEL 0
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
#define PAGE_ALIGN(x) (((x)+4095UL)&~4095UL)
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
#define le64_to_cpu(x) (x)
#define u64_to_user_ptr(x) ((void *)(uintptr_t)(x))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define ERR_PTR(x) ((void *)(intptr_t)(x))
#define PTR_ERR(x) ((int)(intptr_t)(x))
#define IS_ERR(x) ((uintptr_t)(x)>=(uintptr_t)-4095)
struct device {int unused;};
struct drm_device {struct device *dev;};
struct drm_minor {struct drm_device *dev;};
struct drm_file {struct drm_minor *minor;};
struct file {void *private_data;};
struct vm_area_struct;
struct vm_operations_struct {
 void (*open)(struct vm_area_struct *vma);
 void (*close)(struct vm_area_struct *vma);
};
#define VM_WRITE 0x00000002UL
#define VM_MAYWRITE 0x00000020UL
struct vm_area_struct {
 unsigned long vm_start,vm_end,vm_flags,vm_pgoff;
 void *vm_private_data;
 const struct vm_operations_struct *vm_ops;
};
static void vm_flags_clear(struct vm_area_struct *vma,unsigned long flags) {vma->vm_flags&=~flags;}
static void *last_mmap_cpu;
static u64 last_mmap_dma;
static size_t last_mmap_size;
static int dma_mmap_coherent(struct device *d,struct vm_area_struct *vma,void *cpu,u64 dma,size_t size)
{assert(d&&vma&&!vma->vm_pgoff);last_mmap_cpu=cpu;last_mmap_dma=dma;last_mmap_size=size;return 0;}
struct mutex {bool locked;};
struct list_head {struct list_head *next,*prev;};
static void INIT_LIST_HEAD(struct list_head *h) {h->next=h->prev=h;}
static void list_add_tail(struct list_head *n,struct list_head *h)
{n->next=h;n->prev=h->prev;h->prev->next=n;h->prev=n;}
static void list_del(struct list_head *n) {n->prev->next=n->next;n->next->prev=n->prev;}
#define list_for_each_entry_safe(pos,tmp,head,member) \
 for(struct list_head *node=(head)->next,*following=node->next; \
     node!=(head) && ((pos)=container_of(node,__typeof__(*(pos)),member), \
     (tmp)=following==(head)?NULL:container_of(following,__typeof__(*(tmp)),member),1); \
     node=following,following=node->next)
#define list_for_each_entry(pos,head,member) \
 for(struct list_head *node=(head)->next;node!=(head) && ((pos)=container_of(node,__typeof__(*(pos)),member),1);node=node->next)
static bool privileged=true,require_rawio=true,copyin_fail,copyout_fail,memdup_fail,dma_fail;
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
static struct n1x_dla_accel_range writable[64];
static struct n1x_dla_accel_exec req;
static int cases;
static int stop_error,stops;
static int stopped_hardware(struct device *d)
{assert(d==&device && accel.lock.locked && accel.stopped);stops++;return stop_error;}
static int ctrl_start(struct device *d,struct n1x_dla_job *t,const struct n1x_dla_job_ops *ops)
{assert(d==&device && t && ops);t->attempted=true;t->accepted=true;t->error=-EINPROGRESS;return 0;}
static int ctrl_poll(struct device *d,struct n1x_dla_job *t,const struct n1x_dla_job_ops *ops)
{assert(d==&device && t && ops);t->verified=true;t->error=0;return 0;}
static int ctrl_preempt(struct device *d,struct n1x_dla_job *t,const struct n1x_dla_preempt_ops *ops,
 __u64 req_dma,__u32 seq,struct n1x_dla_preempt_result *res)
{assert(d==&device && t && ops && req_dma && seq);
 t->preempt_sequence=seq;t->suspended=true;t->preempt_completed=true;t->error=-EINPROGRESS;
 res->outcome=N1X_DLA_PREEMPT_CHECKPOINT;return 0;}
static int ctrl_resume(struct device *d,struct n1x_dla_job *t,const struct n1x_dla_preempt_ops *ops)
{assert(d==&device && t && ops);t->suspended=false;t->error=-EINPROGRESS;return 0;}
static int ctrl_fail(struct device *d,struct n1x_dla_job *t,int error)
{assert(d==&device && t && error<0);t->error=error;return 0;}
static const struct n1x_dla_accel_control_ops ctrl_ops={
 .start=ctrl_start,.poll=ctrl_poll,.preempt=ctrl_preempt,.resume=ctrl_resume,.fail=ctrl_fail
};
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
 .writable_ranges_ptr=(uintptr_t)writable,.image_bytes=image_bytes,.num_addresses=2,
 .num_writable_ranges=1};
 ranges[0]=(struct n1x_dla_accel_range){0,128};ranges[1]=(struct n1x_dla_accel_range){128,128};
 writable[0]=(struct n1x_dla_accel_range){8,16};
 privileged=true;require_rawio=true;copyin_fail=copyout_fail=memdup_fail=dma_fail=false;
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
  writable[0]=ranges[0];
  check(-ECANCELED);return 0;
 }
 assert(argc==1);
 reset_request();privileged=false;check(-EPERM);assert(!allocations&&!submits);
 struct n1x_dla_accel_info info;
 assert(info_ioctl(&accel.drm,&info,&owner)==-EPERM);
 require_rawio=false;assert(!info_ioctl(&accel.drm,&info,&owner));require_rawio=true;
 struct n1x_dla_accel_read read={0};
 assert(read_ioctl(&accel.drm,&read,&owner)==-EPERM);
 for(int mode=0;mode<11;mode++){
  reset_request();
  switch(mode){case 0:req.flags=2;break;case 1:req.job_id=1;break;
  case 2:req.status=-EIO;break;case 3:req.completed=1;break;case 4:req.image_ptr=0;break;
  case 5:req.ranges_ptr=0;break;case 6:req.writable_ranges_ptr=0;break;case 7:req.image_bytes=0;break;
  case 8:req.image_bytes=N1X_DLA_MAX_IMAGE+1;break;case 9:req.num_addresses=513;break;
  case 10:req.num_writable_ranges=65;break;}
  /* Invalid caller output fields are unchanged on rejected ioctls. */
  assert(exec_ioctl(&accel.drm,&req,&owner)==-EINVAL);cases++;
 }
 reset_request();req.flags=N1X_DLA_EXEC_RESUMABLE;check(-EOPNOTSUPP);
 reset_request();req.num_addresses=0;check(-EINVAL);
 reset_request();req.num_writable_ranges=0;check(-EINVAL);
 reset_request();ranges[1].offset=UINT64_MAX;check(-EINVAL);
 reset_request();ranges[1].bytes=UINT64_MAX;check(-EINVAL);
 reset_request();ranges[1].offset=image_bytes;check(-EINVAL);
 reset_request();ranges[1].bytes=0;check(-EINVAL);
 reset_request();writable[0].offset=image_bytes;check(-EINVAL);
 reset_request();writable[0].bytes=UINT64_MAX;check(-EINVAL);
 reset_request();writable[0]=(struct n1x_dla_accel_range){512,1};check(-EINVAL);
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
 unsigned char replacement=0xcc;
 struct n1x_dla_accel_patch patch={13,1,(uintptr_t)&replacement};
 struct n1x_dla_accel_reexec again={.job_id=1,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
 privileged=false;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EPERM);privileged=true;
 assert(reexec_ioctl(&accel.drm,&again,&other)==-ENOENT);
 again.reserved=1;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EINVAL);again.reserved=0;
 patch.offset=image_bytes;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EINVAL);patch.offset=13;
 patch.offset=100;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EPERM);patch.offset=13;
 memdup_fail=true;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EFAULT);memdup_fail=false;
 read.offset=13;
 backend_attempt=false;backend_error=-ENODEV;
 assert(reexec_ioctl(&accel.drm,&again,&owner)==-ENODEV&&!again.new_job_id);
 backend_error=0;assert(reexec_ioctl(&accel.drm,&again,&owner)==-EIO&&!again.new_job_id);
 backend_attempt=true;
 output=0;assert(!read_ioctl(&accel.drm,&read,&owner)&&output==0x5a);
 u64 previous=1;
 int reuse_allocs=allocations,reuse_frees=frees;
 for(int iteration=0;iteration<64;iteration++){
  replacement=(unsigned char)(0x80+iteration);image[13]=replacement;
  again=(struct n1x_dla_accel_reexec){.job_id=previous,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
  assert(!reexec_ioctl(&accel.drm,&again,&owner));
  assert(again.new_job_id==previous+1&&again.completed&&!again.status);
  assert(accel.count==1&&accel.retained==12288&&allocations==reuse_allocs&&frees==reuse_frees);
  read.job_id=previous;assert(read_ioctl(&accel.drm,&read,&owner)==-ENOENT);
  read.job_id=again.new_job_id;assert(!read_ioctl(&accel.drm,&read,&owner)&&output==0x5a);
  previous=again.new_job_id;
 }
 int savedfrees=frees;postclose(&accel.drm,&owner);
 assert(frees==savedfrees+1&&accel.count==0&&accel.retained==0&&read_ioctl(&accel.drm,&read,&owner)==-ENOENT);
 u64 expected_next=accel.last_id+1;
 reset_request();backend_error=-ETIMEDOUT;check(0);
 assert(req.job_id==expected_next&&!req.completed&&req.status==-ETIMEDOUT&&frees==savedfrees+1);
 read.job_id=req.job_id;assert(read_ioctl(&accel.drm,&read,&owner)==-EIO);
 again=(struct n1x_dla_accel_reexec){.job_id=req.job_id,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
 assert(reexec_ioctl(&accel.drm,&again,&owner)==-EIO);
 postclose(&accel.drm,&owner);
 assert(frees==savedfrees+1&&accel.count==1&&accel.retained==12288);
 assert(!info_ioctl(&accel.drm,&info,&owner)&&info.retained_jobs==1&&info.retained_bytes==12288);
 assert(info.abi_version==2&&info.capabilities==31&&info.reserved==0);
 struct n1x_dla_accel_stats st={.job_id=1,.offset=0,.bytes=28736};
 privileged=false;assert(stats_ioctl(&accel.drm,&st,&owner)==-EPERM);privileged=true;
 assert(stats_ioctl(&accel.drm,&st,&owner)==-ENOENT);
 n1x_dla_accel_set_control(&accel,&ctrl_ops);
 assert(!info_ioctl(&accel.drm,&info,&owner)&&info.capabilities==63);
 image_bytes=32768;reset_request();ranges[0].bytes=32768;ranges[1]=(struct n1x_dla_accel_range){0,128};
 u8 *sb=image+1024;memset(sb,0,28736);
 sb[0]=0x00;sb[1]=0x03;sb[4]=2;sb[8]=4;sb[12]=0x00;sb[13]=0x18;sb[16]=28;
 sb[20]=0x00;sb[21]=0x04;sb[24]=0x40;sb[25]=0x60;
 for(u32 idx=0;idx<4;idx++){u32 off=24640+idx*0x400;memcpy(sb+28+idx*0x1800+0x1108,&off,4);}
 req.flags=N1X_DLA_EXEC_RESUMABLE;check(0);
 u64 resumable_id=req.job_id;assert(resumable_id&&!req.completed&&req.status==0);
 st=(struct n1x_dla_accel_stats){.job_id=resumable_id,.offset=1024,.bytes=28736};
 assert(stats_ioctl(&accel.drm,&st,&owner)==-EIO);
 struct n1x_dla_accel_control ctrl={.job_id=resumable_id,.action=N1X_DLA_CONTROL_START};
 privileged=false;assert(control_ioctl(&accel.drm,&ctrl,&owner)==-EPERM);privileged=true;
 assert(!control_ioctl(&accel.drm,&ctrl,&owner)&&ctrl.accepted&&!ctrl.completed);
 ctrl=(struct n1x_dla_accel_control){.job_id=resumable_id,.action=N1X_DLA_CONTROL_PREEMPT};
 assert(!control_ioctl(&accel.drm,&ctrl,&owner)&&ctrl.suspended&&ctrl.preempt_completed&&ctrl.outcome==N1X_DLA_PREEMPT_CHECKPOINT);
 ctrl=(struct n1x_dla_accel_control){.job_id=resumable_id,.action=N1X_DLA_CONTROL_RESUME};
 assert(!control_ioctl(&accel.drm,&ctrl,&owner)&&!ctrl.suspended&&!ctrl.completed);
 ctrl=(struct n1x_dla_accel_control){.job_id=resumable_id,.action=N1X_DLA_CONTROL_POLL};
 assert(!control_ioctl(&accel.drm,&ctrl,&owner)&&ctrl.completed&&!ctrl.status);
 assert(!stats_ioctl(&accel.drm,&st,&owner)&&st.count==4&&st.metadata_offset==28&&st.data_offset==24640);
 image_bytes=4096;
 /* An uncertain stop must retain every job and block all future execution. */
 stop_error=-ETIMEDOUT;int before_stop=frees;
 assert(n1x_dla_accel_stop(&accel,stopped_hardware)==-ETIMEDOUT && stops==1);
 assert(accel.count==2 && accel.retained==53248 && frees==before_stop);
 n1x_dla_accel_resume(&accel);assert(accel.stopped);
 reset_request();check(-ENODEV);
 again=(struct n1x_dla_accel_reexec){.job_id=read.job_id,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
 assert(reexec_ioctl(&accel.drm,&again,&owner)==-ENODEV);
 /* The callback here models independently established power-off, not recovery.
  * The real bootstrap refuses retry after failure (separately fault-tested). */
 stop_error=0;u64 last=accel.last_id;
 assert(!n1x_dla_accel_stop(&accel,stopped_hardware) && stops==2);
 assert(!accel.count && !accel.retained && frees==before_stop+2 && accel.last_id==last);
 assert(accel.jobs.next==&accel.jobs && accel.jobs.prev==&accel.jobs);
 assert(read_ioctl(&accel.drm,&read,&owner)==-ENOENT);
 postclose(&accel.drm,&owner);assert(!accel.lock.locked);
 assert(!n1x_dla_accel_stop(&accel,stopped_hardware) && frees==before_stop+2);
 n1x_dla_accel_resume(&accel);assert(!accel.stopped);
 /* Verify zero-copy read-only mmap and stop-while-mapped UAF protection. */
 struct drm_minor minor={.dev=&accel.drm};
 owner.minor=&minor;other.minor=&minor;
 struct file filp_owner={.private_data=&owner},filp_other={.private_data=&other};
 backend_error=0;reset_request();check(0);u64 mapped_id=req.job_id;
 struct vm_area_struct vma={.vm_start=0,.vm_end=4096,.vm_flags=VM_MAYWRITE,.vm_pgoff=mapped_id};
 vma.vm_flags|=VM_WRITE;assert(n1x_accel_mmap(&filp_owner,&vma)==-EINVAL);vma.vm_flags=VM_MAYWRITE;
 assert(n1x_accel_mmap(&filp_other,&vma)==-ENOENT);
 vma.vm_end=8192;assert(n1x_accel_mmap(&filp_owner,&vma)==-EINVAL);vma.vm_end=4096;
 assert(!n1x_accel_mmap(&filp_owner,&vma));
 assert(!(vma.vm_flags&VM_MAYWRITE)&&vma.vm_ops==&n1x_accel_vm_ops);
 assert(last_mmap_dma==dma_address+N1X_JOB_PREFIX&&last_mmap_size==4096);
 assert(((u8 *)last_mmap_cpu)[13]==0x5a);
 again=(struct n1x_dla_accel_reexec){.job_id=mapped_id,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
 assert(reexec_ioctl(&accel.drm,&again,&owner)==-EBUSY);
 int frees_before_mapped_stop=frees;
 assert(!n1x_dla_accel_stop(&accel,stopped_hardware));
 /* Mapped job must remain valid across stop() until VMA close (no UAF). */
 assert(accel.count==1&&accel.retained==12288&&frees==frees_before_mapped_stop);
 assert(((u8 *)last_mmap_cpu)[13]==0x5a);
 vma.vm_ops->open(&vma);
 vma.vm_ops->close(&vma);
 assert(accel.count==1&&frees==frees_before_mapped_stop);
 vma.vm_ops->close(&vma);
 assert(!accel.count&&!accel.retained&&frees==frees_before_mapped_stop+1);
 n1x_dla_accel_resume(&accel);assert(!accel.stopped);
 printf("%d exact accel EXEC cases plus READ/INFO/mmap/ownership/close checks passed; no hardware access\n",cases);
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-accel-') as directory:
        code, binary = Path(directory)/'test.c', Path(directory)/'test'
        code.write_text('#define CAPTURE_DMA '+hex(HOST)+'ULL\n'+shim + body + tests)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_dla_task.c'),
            str(ROOT/'driver/n1x_dla_command.c'), str(ROOT/'driver/n1x_dla_statistics.c'),
            '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
