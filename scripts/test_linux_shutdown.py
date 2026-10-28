#!/usr/bin/env python3
"""Compile actual shutdown/lifetime handlers against fault-injected OS edges."""
from pathlib import Path
import re
import subprocess
import tempfile
from npu_trigger import TRIGGERS

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    state = text[text.index('struct n1x_boot_endpoint {'):text.index('static bool n1x_boot_machine(')]
    code = text[text.index('static int n1x_boot_stop_hardware('):text.index('static DEVICE_ATTR_WO(shutdown);')]
    stores = []
    for phase in TRIGGERS:
        if phase == 'shutdown':
            continue
        start = text.index('static ssize_t '+phase+'_store(')
        stores.append(text[start:text.index('static DEVICE_ATTR_WO(', start)])
    names = set(re.findall(r'\b(n1x_boot_\w+)\(\)', ''.join(stores)))-{'n1x_boot_machine'}
    writer_checks = '\n'.join(
        f'assert({phase}_store(&device,NULL,"{payload.decode().strip()}",{len(payload)-1}) == -ESHUTDOWN);'
        for phase, payload in TRIGGERS.items() if phase != 'shutdown')
    writer_harness = 'static bool enable_hfrp;\n'+''.join(
        'static int '+name+'(void) {assert(0);return -EIO;}\n' for name in sorted(names))
    writer_harness += r'''
int n1x_hfrp_query_power_state(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v,u32 p,u32 delay,struct n1x_hfrp_power_state *s,
 struct n1x_hfrp_exchange_result *r) {assert(0);return -EIO;}
''' + ''.join(stores)+'''
static void check_stopped_writers(void) {
 boot_stopping=true;
'''+writer_checks+'\n assert(!boot_lock);\n}\n'
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#include "n1x_hfrp_setup.h"
#include "n1x_dla_boot.h"
#include "n1x_dla_control.h"
#include "n1x_dla_accel.h"
typedef uint8_t u8;typedef uint32_t u32;typedef uint64_t dma_addr_t;
typedef size_t resource_size_t;
#define __iomem
#define SHA256_DIGEST_SIZE 32
#define N1X_BOOT_BYTES 67508
#define N1X_REGION5_ALLOC (1U<<20)
#define N1X_RELU_BYTES 2834432
#define DEFINE_MUTEX(name) int name
#define THIS_MODULE ((void *)1)
#define CAP_SYS_RAWIO 17
#define dev_info(...) ((void)0)
struct device {int refs,id;void *data;};
struct device_attribute {int unused;};
struct n1x_dla_accel {bool locked;int bytes;};
''' + state + r'''
static struct device device={0,0,NULL},sroot_device={0,1,NULL},foreign={0,2,NULL};
static struct n1x_boot_endpoint dla_ep,sroot_ep;
static struct n1x_dla_accel accel;
static bool machine=true,privileged=true;
static int dma_frees,module_refs,pm_puts,mask,gates,accesses,drains,stops;
static int gate_fail,io_fail,drain_error,stop_error;
static bool mask_stuck,confirm=true;
static void mutex_lock(int *p) {assert(!*p);*p=1;}
static void mutex_unlock(int *p) {assert(*p);*p=0;}
static bool capable(int cap) {assert(cap==17);return privileged;}
static bool sysfs_streq(const char *a,const char *b) {return !strcmp(a,b)||!strcmp(a,"stop\n");}
static bool n1x_boot_machine(void) {return machine;}
static void *dev_get_drvdata(struct device *d) {return d->data;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *e)
{assert(boot_lock && (e==&dla_ep||e==&sroot_ep));return ++gates==gate_fail?-EIO:0;}
static int n1x_boot_drain_task_idle(struct n1x_dla_job *j)
{assert(boot_lock && j==&boot_task);drains++;return drain_error;}
static int n1x_boot_read(void *ctx,u32 off,u32 *out)
{assert(boot_lock && ctx==&dla_ep && !boot_shutdown.hardware_off);accesses++;
 if(accesses==io_fail)return -EIO;assert(off==0xd2c||off==0xd28);
 *out=off==0xd2c?0xff:mask;return 0;}
static int n1x_boot_write(void *ctx,u32 off,u32 value)
{assert(boot_lock && ctx==&dla_ep && off==0xd24);accesses++;
 if(!mask_stuck)mask&=~value;return accesses==io_fail?-EIO:0;}
static int n1x_boot_hfrp_read(void *ctx,u32 off,u32 *out)
{(void)ctx;(void)off;(void)out;assert(0);return -EIO;}
static int n1x_boot_hfrp_write(void *ctx,u32 off,u32 value)
{(void)ctx;(void)off;(void)value;assert(0);return -EIO;}
static void n1x_boot_delay(void *ctx,u32 delay) {(void)ctx;(void)delay;assert(0);}
int n1x_hfrp_stop(const struct n1x_dla_io *io,struct n1x_hfrp_channel *ch,
 const struct n1x_hfrp_version *v,u32 polls,u32 delay,struct n1x_hfrp_stop_result *out)
{assert(boot_lock && io->context==&dla_ep && ch==&boot_hfrp.channel && v==&boot_hfrp.version);
 assert(polls==5001 && delay==1000 && !mask && boot_stopping);stops++;
 if(stop_error)return stop_error;out->confirmed=confirm;out->state.valid=1;out->state.raw=7;return 0;}
static void dma_free_coherent(struct device *d,size_t bytes,void *buf,dma_addr_t dma)
{assert(boot_lock && boot_shutdown.hardware_off && !boot_hfrp.powered);
 assert(bytes && buf && dma && (d==&device||d==&sroot_device));dma_frees++;}
static void pm_runtime_put_noidle(struct device *d)
{assert(boot_lock && !accel.locked && !accel.bytes && boot_shutdown.hardware_off);
 assert(d->refs>0 && (d==&device||d==&sroot_device));d->refs--;pm_puts++;}
static void module_put(void *p) {assert(p==THIS_MODULE && module_refs>0);module_refs--;}
int n1x_dla_accel_stop(struct n1x_dla_accel *a,int (*stop)(struct device *))
{assert(a==&accel && !boot_lock && !a->locked);a->locked=true;
 int err=stop(&device);if(!err)a->bytes=0;a->locked=false;return err;}
''' + code + writer_harness + r'''
static void reset(void)
{
 assert(!boot_lock && !accel.locked);
 memset(&boot_shutdown,0,sizeof(boot_shutdown));memset(&boot_hfrp,0,sizeof(boot_hfrp));
 memset(&boot_result,0,sizeof(boot_result));memset(&boot_queue,0,sizeof(boot_queue));
 memset(&boot_ping,0,sizeof(boot_ping));memset(&boot_config,0,sizeof(boot_config));
 memset(&boot_region5,0,sizeof(boot_region5));memset(&boot_config_apply,0,sizeof(boot_config_apply));
 memset(&boot_lpwr,0,sizeof(boot_lpwr));memset(&boot_task,0,sizeof(boot_task));memset(&boot_relu,0,sizeof(boot_relu));
 boot_stopping=false;boot_hfrp_held=boot_upload_held=true;boot_attempted=true;boot_error=0;
 device.refs=2;sroot_device.refs=1;module_refs=2;accel.bytes=4096;
 dla_ep=(struct n1x_boot_endpoint){.dev=&device,.accel=&accel};
 sroot_ep=(struct n1x_boot_endpoint){.dev=&sroot_device,.sroot=true};
 boot_dla=&dla_ep;boot_sroot=&sroot_ep;device.data=&dla_ep;sroot_device.data=&sroot_ep;
 boot_hfrp.powered=boot_hfrp.attempted=true;boot_result.reply_valid=boot_result.idle_observed=1;
 boot_hfrp.gating.request_mask=0x777;
 boot_ping.attempted=boot_ping.verified=true;boot_config.attempted=boot_config.valid=true;
 boot_region5.attempted=boot_region5.registered=true;boot_config_apply.attempted=boot_config_apply.accepted=true;
 boot_lpwr.attempted=boot_lpwr.accepted=true;boot_task.attempted=boot_task.verified=true;
 boot_relu.attempted=boot_relu.verified=true;
 boot_buffer=(void *)1;boot_dma=1;
 boot_ping.buffer=boot_config.buffer=boot_region5.buffer=boot_lpwr.buffer=(void *)1;
 boot_ping.dma=boot_config.dma=boot_region5.dma=boot_lpwr.dma=1;
 boot_task.buffer=boot_relu.buffer=(void *)1;boot_task.dma=boot_relu.dma=1;
 machine=privileged=confirm=true;mask_stuck=false;mask=0xff;
 dma_frees=pm_puts=gates=accesses=drains=stops=0;
 gate_fail=io_fail=drain_error=stop_error=0;
}
static void failed(void)
{
 assert(n1x_boot_stop_device(&device)<0 && boot_stopping);
 assert(!boot_lock && !accel.locked && !dma_frees && !pm_puts && module_refs==2);
 assert(device.refs==2 && sroot_device.refs==1 && accel.bytes==4096);
 assert(boot_buffer && boot_ping.buffer && boot_relu.buffer && !boot_shutdown.released);
 int old_accesses=accesses,old_stops=stops;
 assert(n1x_boot_stop_device(&device)<0 && accesses==old_accesses && stops==old_stops);
}
int main(void)
{
 reset();privileged=false;assert(shutdown_store(&device,NULL,"stop",4)==-EPERM && !gates);
 reset();assert(shutdown_store(&device,NULL,"no",2)==-EPERM && !gates);
 reset();assert(n1x_boot_stop_device(&foreign)==-ENODEV);
 assert(n1x_boot_stop_device(&sroot_device)==-ENODEV && !gates);
 for(int i=0;i<16;i++){
  reset();switch(i){
  case 0:machine=false;break;case 1:boot_hfrp.powered=false;break;
  case 2:boot_hfrp.error=-EIO;break;case 3:boot_queue.poisoned=true;break;
  case 4:boot_queue.active=true;break;case 5:boot_error=-EIO;break;
  case 6:boot_result.reply_valid=false;break;case 7:boot_result.idle_observed=false;break;
  case 8:boot_result.reply.status=1;break;case 9:boot_result.reply.detail=1;break;
  case 10:boot_ping.verified=false;break;case 11:boot_config.valid=false;break;
  case 12:boot_region5.registered=false;break;case 13:boot_config_apply.accepted=false;break;
  case 14:boot_task.verified=false;break;case 15:boot_relu.verified=false;break;}
  failed();assert(!gates && !accesses && !stops);
 }
 for(int i=1;i<=2;i++){reset();gate_fail=i;failed();assert(!accesses);}
 for(int i=1;i<=4;i++){reset();io_fail=i;failed();assert(!stops);}
 reset();drain_error=-EBUSY;failed();assert(!accesses);
 reset();boot_hfrp.gating.request_mask=7;failed();assert(!accesses);
 reset();boot_queue.owner_job=&boot_task;failed();assert(!accesses);
 reset();mask_stuck=true;failed();assert(!stops);
 reset();stop_error=-ETIMEDOUT;failed();assert(stops==1);
 reset();confirm=false;failed();assert(stops==1);
 reset();assert(shutdown_store(&device,NULL,"stop\n",5)==5);
 assert(boot_stopping && boot_shutdown.released && boot_shutdown.hardware_off);
 assert(dma_frees==7 && pm_puts==3 && !module_refs && !device.refs && !sroot_device.refs);
 assert(!boot_buffer && !boot_task.buffer && !boot_ping.buffer && !accel.bytes);
 assert(!boot_hfrp_held && !boot_upload_held && !boot_lock && !accel.locked);
 assert(!n1x_boot_stop_device(&device) && stops==1 && dma_frees==7 && pm_puts==3);
 check_stopped_writers();assert(stops==1 && accesses==4 && dma_frees==7);
 reset();boot_attempted=boot_upload_held=false;boot_hfrp_held=true;
 boot_ping.attempted=boot_config.attempted=boot_region5.attempted=false;
 boot_config_apply.attempted=boot_lpwr.attempted=boot_task.attempted=boot_relu.attempted=false;
 boot_buffer=boot_ping.buffer=boot_config.buffer=boot_region5.buffer=NULL;
 boot_lpwr.buffer=boot_task.buffer=boot_relu.buffer=NULL;
 device.refs=1;sroot_device.refs=0;module_refs=1;accel.bytes=0;
 assert(!n1x_boot_stop_device(&device) && !dma_frees && stops==1 && !drains);
 assert(pm_puts==1 && !device.refs && !module_refs);
 reset();boot_hfrp.attempted=boot_hfrp.powered=boot_attempted=false;
 boot_hfrp_held=boot_upload_held=false;module_refs=device.refs=sroot_device.refs=accel.bytes=0;
 assert(!n1x_boot_stop_device(&device) && !gates && !accesses && !stops && !dma_frees && !pm_puts);
 assert(boot_shutdown.released);
 puts("shutdown: healthy/unused/HFRP-only release, 28 failures, all sysfs writer gates, ownership and no double free passed; no hardware");
 return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-linux-stop-') as tmp:
        path, exe = Path(tmp)/'stop.c', Path(tmp)/'stop'
        path.write_text(harness)
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-Wno-unused-variable', '-Wno-misleading-indentation',
                        '-Wno-sign-compare',
                        '-I'+str(ROOT/'include'), str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
