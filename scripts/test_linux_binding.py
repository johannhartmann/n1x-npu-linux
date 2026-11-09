#!/usr/bin/env python3
"""Test actual component bind/resume lifetime across publication and DRM faults."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    bind = text[text.index('static int n1x_master_bind('):
                text.index('static void n1x_master_unbind(')]
    resume = text[text.index('static int n1x_boot_resume('):
                  text.index('static int n1x_boot_runtime_suspend(')]
    assert '.suppress_bind_attrs = true' in text
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
struct device {void *data;};
struct n1x_dla_accel {int unused;};
struct n1x_boot_endpoint {struct device *dev;bool sroot;struct n1x_dla_accel *accel;};
static struct device dev,provider;
static struct n1x_boot_endpoint dla={.dev=&dev},sroot={.dev=&provider,.sroot=true};
static struct n1x_boot_endpoint *boot_dla=&dla,*boot_sroot=&sroot;
static struct n1x_dla_accel accel;
static struct {bool attempted;struct {unsigned major,minor;} version;struct {unsigned frequency_khz;} policy;} boot_hfrp;
static bool boot_suspended,enable_preempt=true;
static int boot_lock,bind_error,start_error,register_error,module_refs,starts,registrations,unbindings,resumes;
static int n1x_boot_control_ops;
static void mutex_lock(int *lock) {assert(lock==&boot_lock && !*lock);*lock=1;}
static void mutex_unlock(int *lock) {assert(lock==&boot_lock && *lock);*lock=0;}
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
#define THIS_MODULE NULL
#define module_put(module) ((void)(module),--module_refs)
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((long)(intptr_t)(p))
static void *dev_get_drvdata(struct device *d) {return d->data;}
static int component_bind_all(struct device *d,void *data)
{assert(d==&dev && !data && !boot_lock);return bind_error;}
static void component_unbind_all(struct device *d,void *data)
{assert(d==&dev && !data);unbindings++;}
static int n1x_boot_start_once(void)
{assert(boot_lock);starts++;module_refs+=2;boot_hfrp.attempted=true;return start_error;}
static int n1x_boot_accel_submit(void) {return 0;}
static struct n1x_dla_accel *n1x_dla_accel_register(struct device *d,int (*submit)(void))
{assert(d==&dev && submit==n1x_boot_accel_submit && !boot_lock);registrations++;
 return register_error?(void *)(intptr_t)register_error:&accel;}
static void n1x_dla_accel_set_control(struct n1x_dla_accel *a,const void *ops)
{assert(a==&accel && ops==&n1x_boot_control_ops && !boot_lock);}
static void n1x_dla_accel_resume(struct n1x_dla_accel *a)
{assert(a==&accel && !boot_lock);resumes++;}
''' + bind + resume + r'''
static void reset(void)
{assert(!boot_lock);dev.data=&dla;provider.data=&sroot;dla.accel=NULL;
 boot_hfrp.attempted=boot_suspended=false;
 bind_error=start_error=register_error=module_refs=starts=registrations=unbindings=resumes=0;}
int main(void)
{
 reset();assert(!n1x_master_bind(&dev));
 assert(module_refs==2 && starts==1 && registrations==1 && !unbindings && dla.accel==&accel);
 reset();register_error=-ENOMEM;
 assert(!n1x_master_bind(&dev)); /* Publication forbids probe-error devres cleanup. */
 assert(module_refs==2 && starts==1 && registrations==1 && !unbindings && !dla.accel);
 reset();start_error=-ETIMEDOUT;assert(!n1x_master_bind(&dev));
 assert(module_refs==2 && starts==1 && !registrations && !unbindings);
 reset();bind_error=-EIO;assert(n1x_master_bind(&dev)==-EIO);
 assert(!module_refs && !starts && !registrations);
 reset();boot_suspended=true;dla.accel=&accel;
 assert(!n1x_boot_resume(&dev) && module_refs==2 && starts==1 && resumes==1);
 reset();boot_suspended=true;dla.accel=&accel;start_error=-ETIMEDOUT;
 assert(n1x_boot_resume(&dev)==-ETIMEDOUT && module_refs==2 && starts==1 && !resumes);
 puts("NPU bind/resume: publication leases retained, DRM registration fault stays bound: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-binding-') as temporary:
        test, binary = Path(temporary) / 'binding.c', Path(temporary) / 'binding'
        test.write_text(code)
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-fsanitize=address,undefined',
                        str(test), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)


if __name__ == '__main__':
    main()
