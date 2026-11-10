#!/usr/bin/env python3
"""Compile actual shutdown/lifetime handlers against fault-injected OS edges."""
from pathlib import Path
from boot_test_context import context_harness
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def shutdown_harness():
    text = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    state = ""
    dsp_state = ""
    code = text[text.index('static bool n1x_boot_stop_ready('):text.index('static DEVICE_ATTR_WO(shutdown);')]
    code += text[text.index('static void n1x_boot_reset_state('):text.index('static int n1x_sroot_component_bind(')]
    start_fn = text[text.index('static int n1x_boot_start_once('):text.index('static struct attribute *boot_attrs[]')]
    names = set(re.findall(r'\b(n1x_boot_\w+)\(boot\)', start_fn))-{'n1x_boot_start_once'}
    writer_harness = (
        'static int start_step,start_fail_step;\n'
        + ''.join(
            'static int '+name+'(struct n1x_boot_context *boot) {assert(boot==&test_boot && start_fail_step>=0);return ++start_step==start_fail_step?-EREMOTEIO:0;}\n'
            for name in sorted(names))
    )
    writer_harness += start_fn + r'''
static void check_single_start(void) {
 for(int fail=0;fail<=8;fail++){
  boot_stopping=false;privileged=true;
  dla_ep.hfrp=(void *)1;boot_dla=&dla_ep;boot_sroot=&sroot_ep;
  boot_hfrp.attempted=boot_attempted=boot_ping.attempted=boot_region5.attempted=false;
  boot_config.attempted=boot_config_apply.attempted=boot_lpwr.attempted=boot_task.attempted=false;
  boot_hfrp.error=boot_error=boot_ping.error=boot_region5.error=0;
  boot_config.error=boot_config_apply.error=boot_lpwr.error=boot_task.error=0;
  start_step=0;start_fail_step=fail;
  int ret=n1x_boot_start_once(boot);
  assert(ret==(fail?-EREMOTEIO:0) && start_step==(fail?fail:8));
 }
 boot_ping.attempted=true;start_step=0;start_fail_step=0;
 assert(n1x_boot_start_once(boot)==-EALREADY && !start_step);
}
'''
    harness = context_harness() + r'''
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
typedef uint8_t u8;typedef uint32_t u32;
typedef size_t resource_size_t;
#define __iomem
#define WRITE_ONCE(x, val) ((x) = (val))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define SHA256_DIGEST_SIZE 32
#define N1X_REGION5_ALLOC (1U<<20)
#define DEFINE_MUTEX(name) int name
#define THIS_MODULE ((void *)1)
#define CAP_SYS_RAWIO 17
#define dev_info(...) ((void)0)
struct device {int refs,id;void *data;};
struct device_attribute {int unused;};
struct n1x_dla_accel {bool locked;int bytes;};
''' + state + dsp_state + r'''
static struct device device={0,0,NULL},sroot_device={0,1,NULL},foreign={0,2,NULL};
static struct n1x_boot_endpoint dla_ep,sroot_ep;
static struct n1x_dla_accel accel;
static bool privileged=true;
static int dma_frees,module_refs,pm_puts,mask,gates,accesses,drains,stops;
static int gate_fail,io_fail,drain_error,stop_error;
static bool mask_stuck,confirm=true;
static u32 route=0xff;
static void mutex_lock(void *test_mutex) {int *p=test_mutex;assert(!*p);*p=1;}
static void mutex_unlock(void *test_mutex) {int *p=test_mutex;assert(*p);*p=0;}
static bool capable(int cap) {assert(cap==17);return privileged;}
static bool sysfs_streq(const char *a,const char *b) {return !strcmp(a,b)||!strcmp(a,"stop\n");}
static void *dev_get_drvdata(struct device *d) {return d->data;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *e)
{assert(boot_lock && (e==&dla_ep||e==&sroot_ep));return ++gates==gate_fail?-EIO:0;}
static int n1x_boot_drain_task_idle(struct n1x_boot_context *boot, struct n1x_dla_job *j)
{assert(boot_lock && j==&boot_task);drains++;return drain_error;}
static int n1x_boot_read(void *ctx,u32 off,u32 *out)
{assert(boot_lock && ctx==&dla_ep && !boot_shutdown.hardware_off);accesses++;
 if(accesses==io_fail)return -EIO;assert(off==0xd2c||off==0xd28);
 *out=off==0xd2c?route:mask;return 0;}
static int n1x_boot_write(void *ctx,u32 off,u32 value)
{assert(boot_lock && ctx==&dla_ep && off==0xd24);accesses++;
 assert(value==((u32)mask&route));
 if(!mask_stuck)mask&=~(value&~0x01000000U);return accesses==io_fail?-EIO:0;}
static int n1x_boot_hfrp_read(void *ctx,u32 off,u32 *out)
{(void)ctx;(void)off;(void)out;assert(0);return -EIO;}
static int n1x_boot_hfrp_write(void *ctx,u32 off,u32 value)
{(void)ctx;(void)off;(void)value;assert(0);return -EIO;}
static void n1x_boot_delay(void *ctx,u32 delay) {(void)ctx;(void)delay;assert(0);}
int n1x_hfrp_stop(const struct n1x_dla_io *io,struct n1x_hfrp_channel *ch,
 const struct n1x_hfrp_version *v,u32 polls,u32 delay,struct n1x_hfrp_stop_result *out)
{assert(boot_lock && io->context==&dla_ep && ch==&boot_hfrp.channel && v==&boot_hfrp.version);
 assert(polls==5001 && delay==1000 && boot_shutdown.irq_readback_valid && boot_stopping);stops++;
 if(stop_error)return stop_error;out->confirmed=confirm;out->state.valid=1;out->state.raw=7;return 0;}
static void dma_free_coherent(struct device *d,size_t bytes,void *buf,dma_addr_t dma)
{assert(boot_lock && boot_shutdown.hardware_off && !boot_hfrp.powered);
 assert(bytes && buf && dma && (d==&device||d==&sroot_device));dma_frees++;}
static void pm_runtime_put_noidle(struct device *d)
{assert(boot_lock && !accel.locked && !accel.bytes && boot_shutdown.hardware_off);
 assert(d->refs>0 && (d==&device||d==&sroot_device));d->refs--;pm_puts++;}
static void module_put(void *p) {assert(p==THIS_MODULE && module_refs>0);module_refs--;}
void n1x_dla_accel_resume(struct n1x_dla_accel *a) {assert(a==&accel && !boot_lock && !boot_suspended);}
#define dev_err(...) ((void)0)
int n1x_dla_accel_stop(struct n1x_dla_accel *a,int (*stop)(struct device *))
{assert(a==&accel && !boot_lock && !a->locked);a->locked=true;
 int err=stop(&device);if(!err)a->bytes=0;a->locked=false;return err;}
''' + code + writer_harness + text[text.index('static int n1x_boot_prepare('):text.index('static int n1x_boot_runtime_suspend(')] + r'''
static void reset(void)
{test_context_reset();
 assert(!boot_lock && !accel.locked);
 memset(&boot_shutdown,0,sizeof(boot_shutdown));memset(&boot_hfrp,0,sizeof(boot_hfrp));
 memset(&boot_result,0,sizeof(boot_result));memset(&boot_queue,0,sizeof(boot_queue));
 memset(&boot_ping,0,sizeof(boot_ping));memset(&boot_config,0,sizeof(boot_config));
 memset(&boot_region5,0,sizeof(boot_region5));memset(&boot_config_apply,0,sizeof(boot_config_apply));
 memset(&boot_lpwr,0,sizeof(boot_lpwr));memset(&boot_task,0,sizeof(boot_task));
 memset(sroot_dsp_buffers,0,sizeof(sroot_dsp_buffers));
 boot_stopping=boot_sleep_prepared=false;boot_hfrp_held=boot_upload_held=true;boot_attempted=true;boot_error=0;
 device.refs=2;sroot_device.refs=1;module_refs=2;accel.bytes=4096;
 dla_ep=(struct n1x_boot_endpoint){.dev=&device,.accel=&accel};
 sroot_ep=(struct n1x_boot_endpoint){.dev=&sroot_device,.sroot=true};
 boot_dla=&dla_ep;boot_sroot=&sroot_ep;device.data=&dla_ep;sroot_device.data=&sroot_ep;
 boot_hfrp.powered=boot_hfrp.attempted=true;boot_result.reply_valid=boot_result.idle_observed=1;
 boot_hfrp.channel.state=N1X_HFRP_READY;
 boot_hfrp.channel.pending_sequence=N1X_HFRP_NO_SEQUENCE;
 boot_hfrp.gating.request_mask=0x777;
 boot_ping.attempted=boot_ping.verified=true;boot_config.attempted=boot_config.valid=true;
 boot_region5.attempted=boot_region5.registered=true;boot_config_apply.attempted=boot_config_apply.accepted=true;
 boot_lpwr.attempted=boot_lpwr.accepted=true;boot_task.attempted=boot_task.verified=true;
 boot_buffer=(void *)1;boot_dma=1;boot_bytes=67508;
 boot_ping.buffer=boot_config.buffer=boot_region5.buffer=boot_lpwr.buffer=(void *)1;
 boot_ping.dma=boot_config.dma=boot_region5.dma=boot_lpwr.dma=1;
 boot_task.buffer=(void *)1;boot_task.dma=1;
 privileged=confirm=true;mask_stuck=false;mask=0xff;route=0xff;
 dma_frees=pm_puts=gates=accesses=drains=stops=0;
 gate_fail=io_fail=drain_error=stop_error=0;
}
static void failed(void)
{
 assert(n1x_boot_stop_device(&device,false)<0 && boot_stopping);
 assert(!boot_lock && !accel.locked && !dma_frees && !pm_puts && module_refs==2);
 assert(device.refs==2 && sroot_device.refs==1 && accel.bytes==4096);
 assert(boot_buffer && boot_ping.buffer && boot_task.buffer && !boot_shutdown.released);
 int old_accesses=accesses,old_stops=stops;
 assert(n1x_boot_stop_device(&device,false)<0 && accesses==old_accesses && stops==old_stops);
}
int main(void)
{
 reset();boot_sleep_prepared=true;
 assert(shutdown_store(&device,NULL,"stop",4)==-EBUSY);
 assert(!boot_stopping && !gates && !accesses && !stops && !dma_frees);
 boot_sleep_prepared=false;
 reset();privileged=false;assert(shutdown_store(&device,NULL,"stop",4)==-EPERM && !gates);
 reset();assert(shutdown_store(&device,NULL,"no",2)==-EPERM && !gates);
 reset();assert(n1x_boot_stop_device(&foreign,false)==-ENODEV);
 assert(n1x_boot_stop_device(&sroot_device,false)==-ENODEV && !gates);
 reset();boot->client_leases=1;
 assert(n1x_boot_stop_device(&device,false)==-EBUSY);
 assert(!boot_stopping && !boot_shutdown.attempted && !gates && !accesses);
 assert(n1x_boot_prepare(&device)==-EBUSY && !boot_sleep_prepared);

 reset();boot->client_leases=boot->retention_leases=1;
 assert(n1x_boot_prepare(&device)==-EBUSY && !boot_sleep_prepared && !stops);
 boot->ready_clients=1;assert(!n1x_boot_prepare(&device));n1x_boot_complete(&device);
 reset();n1x_boot_hold_publication(boot);boot->client_leases=boot->retention_leases=boot->ready_clients=3;
 for(unsigned i=0;i<3;i++) {
  sroot_dsp_buffers[i].published=sroot_dsp_buffers[i].staging_released=true;
  sroot_dsp_buffers[i].sleep_retained=true;
 }
 assert(!n1x_boot_prepare(&device) && boot_sleep_prepared);
 for(unsigned i=0;i<3;i++) {
  boot->sleeping_clients=i;
  assert(n1x_boot_stop_device(&device,true)==-EBUSY && !stops && !dma_frees);
 }
 boot->sleeping_clients=3;
 assert(n1x_boot_stop_device(&device,false)==-EBUSY && !stops);
 assert(!n1x_boot_stop_device(&device,true) && stops==1 && dma_frees==6);
 assert(boot->lifetime_held && boot->client_leases==3 && boot->retention_leases==3);
 for(unsigned i=0;i<3;i++) assert(sroot_dsp_buffers[i].published);

 for(unsigned fail=0;fail<=8;fail++) {
  reset();dla_ep.hfrp=(void *)1;n1x_boot_hold_publication(boot);
  boot->client_leases=boot->retention_leases=boot->ready_clients=boot->sleeping_clients=1;
  sroot_dsp_buffers[0].published=sroot_dsp_buffers[0].staging_released=true;
  sroot_dsp_buffers[0].sleep_retained=true;
  assert(!n1x_boot_prepare(&device) && !n1x_boot_suspend(&device));
  assert(boot_suspended && boot->lifetime_held && boot->client_leases==1);
  assert(sroot_dsp_buffers[0].published && boot->sleeping_clients==1);
  start_step=0;start_fail_step=fail;
  int resumed=n1x_boot_resume(&device);
  if(resumed!=(fail?-EREMOTEIO:0))fprintf(stderr,"resume fail=%u error=%d step=%d suspended=%d\n",fail,resumed,start_step,boot_suspended);
  assert(resumed==(fail?-EREMOTEIO:0));
  assert(boot_suspended==(fail!=0) && boot->client_leases==1 && boot->lifetime_held);
  n1x_boot_complete(&device);assert(!boot_sleep_prepared);
 }
 for(unsigned i=0;i<ARRAY_SIZE(sroot_dsp_buffers);i++) {
  reset();sroot_dsp_buffers[i].published=true;
  sroot_dsp_buffers[i].staging_released=true;assert(!sroot_dsp_buffers[i].cpu && !sroot_dsp_buffers[i].dma);
  assert(n1x_boot_stop_device(&device,false)==-EBUSY);
  assert(!boot_stopping && !boot_shutdown.attempted && !boot_lock && !accel.locked);
  assert(!gates && !accesses && !stops && !dma_frees && !pm_puts && module_refs==2);
  assert(accel.bytes==4096 && boot_buffer && boot_task.buffer);
 }
 for(int i=0;i<16;i++){
  reset();switch(i){
  case 0:boot_hfrp.powered=false;break;
  case 1:boot_hfrp.error=-EIO;break;case 2:boot_queue.poisoned=true;break;
  case 3:boot_queue.active=true;break;case 4:boot_error=-EIO;break;
  case 5:boot_result.reply_valid=false;break;case 6:boot_result.idle_observed=false;break;
  case 7:boot_result.reply.status=1;break;case 8:boot_result.reply.detail=1;break;
  case 9:boot_ping.verified=false;break;case 10:boot_config.valid=false;break;
  case 11:boot_region5.registered=false;break;case 12:boot_config_apply.accepted=false;break;
  case 13:boot_task.verified=false;break;
  case 14:boot_hfrp.channel.state=N1X_HFRP_FAILED;break;
  case 15:boot_hfrp.channel.pending_sequence=1;break;}
  failed();assert(!gates && !accesses && !stops);
 }
 for(int i=1;i<=2;i++){reset();gate_fail=i;failed();assert(!accesses);}
 for(int i=1;i<=4;i++){reset();io_fail=i;failed();assert(!stops);}
 reset();drain_error=-EBUSY;failed();assert(!accesses);
 reset();boot_hfrp.gating.request_mask=7;failed();assert(!accesses);
 reset();boot_queue.owner_job=&boot_task;failed();assert(!accesses);
 /* Match the physical raw readback: bit 24 persists. No Linux IRQ handler
  * is registered; this must reach the independent HFRP shutdown checks. */
 reset();route=0x010801d0;mask=0x01001e05;
 assert(!n1x_boot_stop_device(&device,false) && stops==1 && dma_frees==6);
 assert(boot_shutdown.irq_owned==route && boot_shutdown.irq_mask_before==0x01001e05);
 assert(boot_shutdown.irq_mask_after==0x01001e05 && boot_shutdown.irq_readback_valid);
 reset();mask_stuck=true;stop_error=-ETIMEDOUT;failed();assert(stops==1);
 reset();mask_stuck=true;confirm=false;failed();assert(stops==1);
 reset();stop_error=-ETIMEDOUT;failed();assert(stops==1);
 reset();confirm=false;failed();assert(stops==1);
 reset();assert(shutdown_store(&device,NULL,"stop\n",5)==5);
 assert(boot_stopping && boot_shutdown.released && boot_shutdown.hardware_off);
 assert(dma_frees==6 && pm_puts==3 && !module_refs && !device.refs && !sroot_device.refs);
 assert(!boot_buffer && !boot_task.buffer && !boot_ping.buffer && !accel.bytes);
 assert(!boot_hfrp_held && !boot_upload_held && !boot_lock && !accel.locked);
 assert(!n1x_boot_stop_device(&device,false) && stops==1 && dma_frees==6 && pm_puts==3);
 assert(stops==1 && accesses==4 && dma_frees==6);
 check_single_start();
 reset();boot_attempted=boot_upload_held=false;boot_hfrp_held=true;
 boot_ping.attempted=boot_config.attempted=boot_region5.attempted=false;
 boot_config_apply.attempted=boot_lpwr.attempted=boot_task.attempted=false;
 boot_buffer=boot_ping.buffer=boot_config.buffer=boot_region5.buffer=NULL;
 boot_lpwr.buffer=boot_task.buffer=NULL;
 device.refs=1;sroot_device.refs=0;module_refs=1;accel.bytes=0;
 assert(!n1x_boot_stop_device(&device,false) && !dma_frees && stops==1 && !drains);
 assert(pm_puts==1 && !device.refs && !module_refs);
 reset();boot_hfrp.attempted=boot_hfrp.powered=boot_attempted=false;
 boot_hfrp_held=boot_upload_held=false;module_refs=device.refs=sroot_device.refs=accel.bytes=0;
 assert(!n1x_boot_stop_device(&device,false) && !gates && !accesses && !stops && !dma_frees && !pm_puts);
 assert(boot_shutdown.released);
 puts("shutdown: healthy/unused/HFRP-only release, raw-mask readback regression, failure cases, all sysfs writer gates, ownership and no double free passed; no hardware");
 return 0;
}
'''
    return harness


def main():
    harness = shutdown_harness()
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
