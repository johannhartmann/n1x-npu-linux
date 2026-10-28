#!/usr/bin/env python3
"""Check load-before-acquire in both actual Linux fence callbacks, without DMA."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    boot = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    accel = (ROOT / 'driver/n1x_dla_accel.c').read_text()
    callbacks = boot[boot.index('static int n1x_boot_read_job_fence('):
                     boot.index('static int n1x_boot_run_task(')]
    callbacks += accel[accel.index('static int read_fence('):
                       accel.index('static int info_ioctl(')]
    constants = accel[accel.index('#define N1X_JOB_PREFIX'):
                      accel.index('struct n1x_accel_job {')]
    harness = constants + r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "n1x_dla_job.h"
typedef uint32_t u32;
typedef uint32_t __le32;
struct n1x_accel_job {struct n1x_dla_job task;};
static unsigned char backing[4096] __attribute__((aligned(256)));
static u32 phase,expected,*observation,*expected_address;
static u32 read_once(u32 *p)
{assert(phase==0 && p==expected_address);phase=1;return *p;}
static void dma_rmb(void)
{assert(phase==1 && *observation==expected);phase=2;}
#define READ_ONCE(x) read_once(&(x))
#define le32_to_cpu(x) (x)
''' + callbacks + r'''
int main(void)
{const u32 values[]={0,1,0x80000000,0xffffffff};
 struct n1x_accel_job job={.task={.buffer=backing}};
 for(unsigned n=0;n<sizeof(values)/sizeof(values[0]);n++)for(int kind=0;kind<2;kind++){
 expected=values[n];expected_address=(u32 *)(backing+(kind?N1X_JOB_FENCE:768));
 *expected_address=expected;phase=0;
 u32 value=~expected;observation=&value;
 int err=kind?read_fence(&job,&value):n1x_boot_read_job_fence(&job.task,&value);
 assert(!err && phase==2 && value==expected);}
 puts("8 fence load/acquire ordering cases passed; no hardware or memory-reordering simulation");}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-fence-order-') as tmp:
        code, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(ROOT / 'include'),
            str(code), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
