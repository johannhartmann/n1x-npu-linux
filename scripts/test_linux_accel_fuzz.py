#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Seeded property tests of actual EXEC/READ/REEXEC/mmap/close C functions."""
from test_linux_accel import main

HELPERS = r'''
static u64 random_state;
static u64 draw(void)
{random_state^=random_state<<13;random_state^=random_state>>7;
 random_state^=random_state<<17;return random_state;}
static u64 boundary(u64 size)
{const u64 values[]={0,1,3,127,255,size-1,size,size+1,UINT64_MAX,UINT64_MAX-4095};
 return draw()%3 ? values[draw()%(sizeof(values)/sizeof(values[0]))] : draw();}
static bool within(u64 offset,u64 bytes,u64 base,u64 length)
{/* Deliberately use wide end arithmetic, independently of the driver's subtraction checks. */
 return bytes && offset>=base && (__uint128_t)offset+bytes<=(__uint128_t)base+length;}
static unsigned random_submits;
static int random_backend(struct device *dev,struct n1x_dla_job *task,
 const struct n1x_dla_job_ops *ops)
{assert(dev==&device && accel.lock.locked && task && ops);
 random_submits++;u8 *p=task->buffer;
 assert(p[8]==2 && word64(p+21)==task->dma+N1X_JOB_ADDRESSES);
 assert(word64(p+65)==task->dma+N1X_JOB_FENCE);
 for(unsigned i=0;i<req.num_addresses;i++)
  assert(word64(p+N1X_JOB_ADDRESSES+8*i)==task->dma+N1X_JOB_PREFIX+ranges[i].offset);
 task->attempted=true;task->verified=true;return 0;}
'''

CASES = r'''
 unsigned accepted=0,rejected=0,mapped=0,patched=0;
 const u64 seeds[]={1,0x61742,0x20251722,0xdeadbeef12345678ULL};
 struct drm_minor minor={.dev=&accel.drm};owner.minor=other.minor=&minor;
 struct file filp={.private_data=&owner};
 accel.submit=random_backend;
 for(unsigned seed=0;seed<sizeof(seeds)/sizeof(seeds[0]);seed++) {
  random_state=seeds[seed];
  for(unsigned iteration=0;iteration<4096;iteration++) {
   image_bytes=512U<<(draw()%5);reset_request();
   accel.stopped=accel.unplugged=accel.stop_failed=false;
   /* Begin with valid varied ranges, then mutate one boundary or gate. */
   ranges[0].offset=draw()%(image_bytes/2);
   ranges[0].bytes=1+draw()%(image_bytes-ranges[0].offset);
   ranges[1].offset=draw()%image_bytes;
   ranges[1].bytes=1+draw()%(image_bytes-ranges[1].offset);
   writable[0].offset=ranges[0].offset+draw()%ranges[0].bytes;
   writable[0].bytes=1+draw()%(ranges[0].bytes-(writable[0].offset-ranges[0].offset));
   switch(draw()%16) {
   case 0:req.flags=2U<<(draw()%30);break;
   case 1:req.num_addresses=draw()%2?0:513;break;
   case 2:req.num_writable_ranges=draw()%2?0:65;break;
   case 3:req.job_id=draw()|1;break;
   case 4:req.image_bytes=draw()%2?0:N1X_DLA_MAX_IMAGE+1;break;
   case 5:ranges[0].offset=boundary(image_bytes);break;
   case 6:ranges[1].bytes=boundary(image_bytes);break;
   case 7:writable[0].offset=boundary(image_bytes);break;
   case 8:writable[0].bytes=boundary(image_bytes);break;
   case 9:accel.stopped=true;break;
   case 10:privileged=false;break;
   case 11:req.completed=1;break;
   case 12:dma_address=draw()%2?1:1ULL<<36;break;
   case 13:copyin_fail=true;break;
   default:break;
   }
   bool valid=privileged && !req.flags && !req.job_id && !req.completed &&
    req.image_bytes && req.image_bytes<=N1X_DLA_MAX_IMAGE &&
    req.num_addresses==2 && req.num_writable_ranges==1 && !accel.stopped && !copyin_fail &&
    !(dma_address&4095) && dma_address &&
    (__uint128_t)dma_address+PAGE_ALIGN(req.image_bytes+N1X_JOB_PREFIX)<=((__uint128_t)1<<36);
   for(unsigned i=0;i<2;i++)valid=valid && within(ranges[i].offset,ranges[i].bytes,0,req.image_bytes);
   valid=valid && within(writable[0].offset,writable[0].bytes,0,req.image_bytes) &&
    (within(writable[0].offset,writable[0].bytes,ranges[0].offset,ranges[0].bytes) ||
     within(writable[0].offset,writable[0].bytes,ranges[1].offset,ranges[1].bytes));
   int before_alloc=allocations,before_free=frees;unsigned before_submit=random_submits;
   int result=exec_ioctl(&accel.drm,&req,&owner);
   if((result==0)!=valid) {
    fprintf(stderr,"seed=%llu iteration=%u result=%d expected_valid=%d\n",
     (unsigned long long)seeds[seed],iteration,result,valid);abort();
   }
   assert(!accel.lock.locked && random_submits-before_submit==(unsigned)valid);
   if(!valid) {
    rejected++;assert(result<0 && !accel.count && !accel.retained);
    assert(allocations-before_alloc==frees-before_free);
   } else {
    accepted++;assert(req.job_id && req.completed && !req.status && accel.count==1);
    copyin_fail=false;
    u8 output[8192]={0};
    struct n1x_dla_accel_read read={.job_id=req.job_id,.offset=draw()%image_bytes,
     .bytes=1,.data_ptr=(uintptr_t)output};
    assert(read_ioctl(&accel.drm,&read,&other)==-ENOENT);
    assert(!read_ioctl(&accel.drm,&read,&owner) && output[0]==image[read.offset]);
    read.offset=UINT64_MAX;assert(read_ioctl(&accel.drm,&read,&owner)==-EINVAL);
    /* REEXEC cannot alter bytes outside the registered writable interval. */
    struct n1x_dla_accel_patch patch={.offset=draw()%image_bytes,.bytes=1,
     .data_ptr=(uintptr_t)image};
    struct n1x_dla_accel_reexec re={.job_id=req.job_id,.patches_ptr=(uintptr_t)&patch,.num_patches=1};
    bool allowed=within(patch.offset,patch.bytes,writable[0].offset,writable[0].bytes);
    int ret=reexec_ioctl(&accel.drm,&re,&owner);
    assert(ret==(allowed?0:-EPERM));
    if(allowed){assert(re.new_job_id>req.job_id);req.job_id=re.new_job_id;patched++;}
    if(draw()%2) {
     struct vm_area_struct vma={.vm_end=PAGE_ALIGN(image_bytes),.vm_flags=VM_MAYWRITE,.vm_pgoff=req.job_id};
     assert(!n1x_accel_mmap(&filp,&vma));mapped++;
     postclose(&accel.drm,&owner);
     assert(accel.count==1 && frees==before_free && vma.vm_private_data);
     /* A forked VMA must keep storage alive after file close and first unmap. */
     vma.vm_ops->open(&vma);vma.vm_ops->close(&vma);
     assert(accel.count==1);vma.vm_ops->close(&vma);
    } else postclose(&accel.drm,&owner);
    assert(!accel.count && !accel.retained && allocations-before_alloc==frees-before_free);
   }
   assert(accel.jobs.next==&accel.jobs && accel.jobs.prev==&accel.jobs);
  }
 }
 printf("Seeded accel properties: 16384 cases, %u accepted, %u rejected, %u mmap lifetimes, %u REEXEC patches; ASan/UBSan, no hardware\n",
  accepted,rejected,mapped,patched);
'''


if __name__ == '__main__':
    main(CASES, extra_helpers=HELPERS, sanitize=True)
