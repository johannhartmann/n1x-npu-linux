#!/usr/bin/env python3
"""Check actual accel-to-bootstrap readiness/serialization boundary offline."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = text[text.index('static int n1x_boot_accel_submit('):text.index('static int n1x_boot_probe(')]
    shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "n1x_dla_job.h"
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device,foreign;
static struct n1x_boot_endpoint ep={&device},*boot_dla,*boot_sroot;
static bool enable_accel,machine_ok,boot_relu_numeric,boot_stopping;
static struct n1x_dla_job boot_task,boot_relu;
static struct n1x_dla_job_queue boot_queue;
static struct {bool accepted;} boot_lpwr;
static struct {bool powered;struct {bool boot_tables_valid;} policy;} boot_hfrp;
static int boot_lock,gate_error,drain_error,run_error,gates,drains,runs,cases;
static void mutex_lock(int *p) {assert(!*p);*p=1;}
static void mutex_unlock(int *p) {assert(*p);*p=0;}
static bool n1x_boot_machine(void) {assert(boot_lock);return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *p)
{assert(boot_lock&&p==&ep);gates++;return gate_error;}
static int n1x_boot_drain_task_idle(struct n1x_dla_job *job)
{assert(boot_lock&&!job->attempted);drains++;return drain_error;}
static int n1x_boot_write(void *p,__u32 a,__u32 b) {(void)p;(void)a;(void)b;assert(0);return 0;}
static int n1x_boot_read(void *p,__u32 a,__u32 *b) {(void)p;(void)a;(void)b;assert(0);return 0;}
static void n1x_boot_delay(void *p,__u32 a) {(void)p;(void)a;assert(0);}
int n1x_dla_job_execute(struct n1x_dla_job_queue *q,const struct n1x_dla_io *io,
                       const struct n1x_dla_job_ops *ops,struct n1x_dla_job *job)
{assert(boot_lock&&q==&boot_queue&&io->context==boot_dla&&ops->context==job);
 assert(gates==1&&drains==1);runs++;job->attempted=true;return run_error;}
'''
    tests = r'''
static void reset(void)
{boot_stopping=false;enable_accel=machine_ok=boot_relu_numeric=true;boot_dla=boot_sroot=&ep;
 boot_task.verified=boot_lpwr.accepted=boot_hfrp.powered=boot_hfrp.policy.boot_tables_valid=true;
 boot_relu.attempted=false;gates=drains=runs=gate_error=drain_error=run_error=0;}
static void check(struct device *dev,int expected,int g,int d,int r)
{struct n1x_dla_job job={0};struct n1x_dla_job_ops ops={.context=&job};
 assert(n1x_boot_accel_submit(dev,&job,&ops)==expected);
 assert(!boot_lock&&gates==g&&drains==d&&runs==r&&job.attempted==(r!=0));cases++;}
int main(void)
{
 for(int mode=0;mode<10;mode++){
  reset();
  switch(mode){case 0:enable_accel=false;break;case 1:machine_ok=false;break;
  case 2:boot_dla=NULL;break;case 3:boot_sroot=NULL;break;case 4:boot_task.verified=false;break;
  case 5:boot_lpwr.accepted=false;break;case 6:boot_hfrp.powered=false;break;
  case 7:boot_hfrp.policy.boot_tables_valid=false;break;
  case 8:boot_relu.attempted=true;boot_relu_numeric=false;break;case 9:break;}
  check(mode==9?&foreign:&device,-ENODEV,0,0,0);
 }
 reset();gate_error=-EOPNOTSUPP;check(&device,-EOPNOTSUPP,1,0,0);
 reset();boot_stopping=true;check(&device,-ENODEV,0,0,0);
 reset();drain_error=-ENOTRECOVERABLE;check(&device,-ENOTRECOVERABLE,1,1,0);
 reset();run_error=-ETIMEDOUT;check(&device,-ETIMEDOUT,1,1,1);
 reset();check(&device,0,1,1,1);
 reset();boot_relu.attempted=true;check(&device,0,1,1,1);
 printf("%d actual accel backend readiness/locking cases passed; no hardware access\n",cases);
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-accel-backend-') as tmp:
        source, binary = Path(tmp)/'test.c', Path(tmp)/'test'
        source.write_text(shim+function+tests)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
