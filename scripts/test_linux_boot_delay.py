#!/usr/bin/env python3
"""Exercise the real delay callback without accessing unpowered DLA registers."""
import os
from pathlib import Path
from boot_test_context import context_harness
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    callback = source[source.index('static void n1x_boot_delay('):
                      source.index('static int n1x_boot_board_policy(')]
    harness = context_harness() + r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
static struct n1x_boot_endpoint dla, sroot;

static bool opt_enable_irq;

static int reads, writes, sleeps, waits, read_error;
#define READ_ONCE(x) (x)
#define max(a,b) ((a)>(b)?(a):(b))
static int atomic_read(int *p) {return *p;}
static unsigned long usecs_to_jiffies(u32 us) {return us / 1000;}
#define wait_event_timeout(q, condition, timeout) \
    do {assert(&(q)==&boot_waitq); (void)(condition); assert(timeout>=1); waits++;} while(0)
static int n1x_boot_read(void *ctx,u32 offset,u32 *value)
{assert(ctx==boot_dla && boot_hfrp.powered && !boot_stopping);
 assert(offset==0xd2c || offset==0xd28); reads++; *value=0xc0; return read_error;}
static void n1x_boot_write_irq_enable(void *ctx,u32 value)
{assert(ctx==boot_dla && boot_hfrp.powered && !boot_stopping && value==0xc0);writes++;}
static void usleep_range(u32 minimum,u32 maximum)
{assert(maximum==minimum+100);sleeps++;}
''' + callback + r'''
static void reset(void)
{test_context_reset();boot_dla=&dla;dla.boot=sroot.boot=boot;opt_enable_irq=boot_irq_registered=true;boot_hfrp.powered=true;boot_stopping=false;
 reads=writes=sleeps=waits=read_error=0;}
static void fallback(void *ctx,u32 delay)
{n1x_boot_delay(ctx,delay);assert(sleeps==1 && !reads && !writes && !waits);}
int main(void)
{
 reset();boot_hfrp.powered=false;fallback(&dla,1000);
 reset();boot_stopping=true;fallback(&dla,1000);
 reset();opt_enable_irq=false;fallback(&dla,1000);
 reset();boot_irq_registered=false;fallback(&dla,1000);
 reset();fallback(&sroot,1000);
 reset();fallback(&dla,999);
 reset();n1x_boot_delay(&dla,1000);assert(reads==2 && writes==1 && waits==1 && !sleeps);
 reset();read_error=-1;n1x_boot_delay(&dla,1000);
 assert(reads==1 && !writes && waits==1 && !sleeps);
 puts("8 NPU delay cases passed: no DLA MMIO before power-on or during shutdown");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-boot-delay-') as temp:
        code, binary = Path(temp) / 'test.c', Path(temp) / 'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) +
                       ['-I'+str(ROOT/'include'), '-O2', '-Wall', '-Wextra', '-Werror', str(code), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
