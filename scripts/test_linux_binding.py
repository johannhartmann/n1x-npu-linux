#!/usr/bin/env python3
"""Test actual component bind/resume lifetime across publication and DRM faults."""
from pathlib import Path
from boot_test_context import context_harness
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    bind = text[text.index('static int n1x_master_bind('):
                text.index('static void n1x_master_unbind(')]
    component = text[text.index('static int n1x_sroot_component_bind('):
                     text.index('static const struct component_ops n1x_sroot_component_ops =')]
    unbind = text[text.index('static void n1x_master_unbind('):
                  text.index('static const struct component_master_ops n1x_master_ops =')]
    resume = text[text.index('static int n1x_boot_resume('):
                  text.index('static int n1x_boot_runtime_suspend(')]
    assert '.suppress_bind_attrs = true' in text
    code = context_harness() + r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
struct device {void *data,*driver;unsigned refs;};
struct device_link {int unused;};
struct n1x_dla_accel {int unused;};

static struct device dev,provider,foreign;
static int driver,other_driver;
static struct device_link link;
static struct n1x_boot_endpoint dla={.dev=&dev},sroot={.dev=&provider,.sroot=true};

static struct n1x_dla_accel accel;

static bool enable_preempt=true;
static int bind_error,start_error,register_error,module_refs,starts,registrations,unbindings,resumes;
static bool link_error;

static int links,additions,deletions,stops,unregistrations,resets;
static int n1x_boot_control_ops;
static void mutex_lock(void *test_mutex) {int *lock=test_mutex;assert((lock==&boot_lock || lock==&n1x_contexts_lock.value) && !*lock);*lock=1;}
static void mutex_unlock(void *test_mutex) {int *lock=test_mutex;assert((lock==&boot_lock || lock==&n1x_contexts_lock.value) && *lock);*lock=0;}
#define WRITE_ONCE(x,v) ((x)=(v))
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
#define THIS_MODULE NULL
#define module_put(module) ((void)(module),--module_refs)
static void *dev_get_drvdata(struct device *d) {return d->data;}
static struct device *get_device(struct device *d) {if(d)d->refs++;return d;}
static void put_device(struct device *d) {if(d){assert(d->refs);d->refs--;}}
static unsigned drains;
static void synchronize_irq(int irq) {assert(irq==234 && !boot_lock && !dla.boot);drains++;}
#define DL_FLAG_STATELESS 1
static struct device_link *device_link_add(struct device *consumer,struct device *supplier,unsigned flags)
{assert(consumer==&dev && supplier==&provider && flags==DL_FLAG_STATELESS);
 assert(!boot_lock && !starts && !links);additions++;
 if(link_error)return NULL;
 links++;return &link;}
static void device_link_del(struct device_link *l)
{assert(l==&link && links==1 && !boot_lock);links--;deletions++;}
''' + component + r'''
static int component_bind_all(struct device *d,void *data)
{assert(d==&dev && !boot_lock);return bind_error?bind_error:n1x_sroot_component_bind(&provider,d,data);}
static void component_unbind_all(struct device *d,void *data)
{assert(d==&dev && data==&dla && !boot_lock);unbindings++;n1x_sroot_component_unbind(&provider,d,data);}
static int n1x_boot_start_once(struct n1x_boot_context *boot)
{assert(boot_lock && links==1 && dla.sroot_link==&link);
 starts++;module_refs+=2;boot_hfrp.attempted=true;return start_error;}
static int n1x_boot_accel_submit(void) {return 0;}
static struct n1x_dla_accel *n1x_dla_accel_register(struct device *d,int (*submit)(void))
{assert(d==&dev && submit==n1x_boot_accel_submit && !boot_lock);registrations++;
 return register_error?(void *)(intptr_t)register_error:&accel;}
static void n1x_dla_accel_set_control(struct n1x_dla_accel *a,const void *ops)
{assert(a==&accel && ops==&n1x_boot_control_ops && !boot_lock);}
static void n1x_dla_accel_resume(struct n1x_dla_accel *a)
{assert(a==&accel && !boot_lock);resumes++;}
static int n1x_boot_stop_device(struct device *d,bool sleep)
{assert(d==&dev && !sleep && !boot_lock && links==1);stops++;
 boot_shutdown.attempted=boot_shutdown.released=true;return 0;}
static void n1x_dla_accel_unregister(struct n1x_dla_accel *a)
{assert(a==&accel && !boot_lock && links==1 && boot_shutdown.released);unregistrations++;}
static void n1x_boot_reset_state(struct n1x_boot_context *boot)
{assert(boot_lock && boot_shutdown.released && links==1);boot_hfrp.attempted=false;resets++;}
''' + bind + unbind + resume + r'''
static void reset(void)
{test_context_reset();boot_dla=&dla;boot_sroot=&sroot;dla.boot=sroot.boot=boot;assert(!boot_lock);dev.data=&dla;provider.data=&sroot;dla.accel=NULL;
 dev.driver=provider.driver=foreign.driver=&driver;
 dev.refs=provider.refs=foreign.refs=1;drains=0;dla.irq=0;
 dla.sroot=false;sroot.sroot=true;dla.dev=&dev;sroot.dev=&provider;dla.sroot_link=NULL;
 boot_attempted=link_error=boot_shutdown.attempted=boot_shutdown.released=false;
 links=additions=deletions=stops=unregistrations=resets=0;
 boot_hfrp.attempted=boot_suspended=false;
 bind_error=start_error=register_error=module_refs=starts=registrations=unbindings=resumes=0;}
int main(void)
{
 reset();dla.irq=234;assert(!n1x_master_bind(&dev));
 assert(module_refs==2 && starts==1 && registrations==1 && !unbindings && dla.accel==&accel);
 assert(links==1 && additions==1 && !deletions);
 n1x_master_unbind(&dev);
 assert(stops==1 && unregistrations==1 && unbindings==1 && resets==1);
 assert(drains==1 && dev.refs==1 && !boot->consumer && !dla.boot);
 assert(!links && deletions==1 && !dla.sroot_link && !dla.accel);
 n1x_sroot_component_unbind(&provider,&dev,&dla);assert(deletions==1);
 reset();register_error=-ENOMEM;
 assert(!n1x_master_bind(&dev)); /* Publication forbids probe-error devres cleanup. */
 assert(module_refs==2 && starts==1 && registrations==1 && !unbindings && !dla.accel);
 assert(links==1 && !deletions); /* Link remains across post-publication DRM failure. */
 reset();start_error=-ETIMEDOUT;assert(!n1x_master_bind(&dev));
 assert(module_refs==2 && starts==1 && !registrations && !unbindings);
 reset();bind_error=-EIO;assert(n1x_master_bind(&dev)==-EIO);
 assert(!module_refs && !starts && !registrations);
 reset();link_error=true;assert(n1x_master_bind(&dev)==-ENOMEM);
 assert(additions==1 && !links && !starts && !module_refs && !deletions);
 reset();provider.data=NULL;assert(n1x_master_bind(&dev)==-EINVAL);
 assert(!additions && !starts && !module_refs);
 reset();provider.driver=&other_driver;assert(n1x_master_bind(&dev)==-ENODEV);
 assert(!additions && !starts && !module_refs);
 reset();assert(n1x_sroot_component_bind(&provider,&dev,NULL)==-EINVAL);
 assert(n1x_sroot_component_bind(&provider,&foreign,&dla)==-EINVAL);
 dla.sroot=true;assert(n1x_sroot_component_bind(&provider,&dev,&dla)==-EINVAL);dla.sroot=false;
 sroot.sroot=false;assert(n1x_sroot_component_bind(&provider,&dev,&dla)==-EINVAL);sroot.sroot=true;
 sroot.dev=&foreign;assert(n1x_sroot_component_bind(&provider,&dev,&dla)==-EINVAL);sroot.dev=&provider;
 assert(!additions && !starts && !module_refs);
 assert(!n1x_sroot_component_bind(&provider,&dev,&dla));
 assert(n1x_sroot_component_bind(&provider,&dev,&dla)==-EBUSY && additions==1);
 n1x_sroot_component_unbind(&provider,&dev,&dla);assert(!links && deletions==1);
 assert(!n1x_sroot_component_bind(&provider,&dev,&dla) && !boot_stopping);
 n1x_sroot_component_unbind(&provider,&dev,&dla);
 for(unsigned bad=0;bad<3;bad++) {
  reset();boot_hfrp.attempted=bad==0;boot_attempted=bad==1;boot->client_leases=bad==2;
  assert(n1x_sroot_component_bind(&provider,&dev,&dla)==-ENOTRECOVERABLE && !additions);
 }
 reset();boot_suspended=true;dla.accel=&accel;
 assert(!n1x_sroot_component_bind(&provider,&dev,&dla));
 assert(!n1x_boot_resume(&dev) && module_refs==2 && starts==1 && resumes==1);
 reset();boot_suspended=true;dla.accel=&accel;start_error=-ETIMEDOUT;
 assert(!n1x_sroot_component_bind(&provider,&dev,&dla));
 assert(n1x_boot_resume(&dev)==-ETIMEDOUT && module_refs==2 && starts==1 && !resumes);
 puts("NPU binding: PM link precedes publication, identity/allocation faults fail closed, cleanup is balanced, leases survive DRM/resume faults: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-binding-') as temporary:
        test, binary = Path(temporary) / 'binding.c', Path(temporary) / 'binding'
        test.write_text(code)
        subprocess.run(['cc', '-I'+str(ROOT/'include'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-fsanitize=address,undefined',
                        str(test), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)


if __name__ == '__main__':
    main()
