#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise actual startup gates and leases with fault-injected kernel edges."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from boot_test_context import context_harness

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    prepare = source[source.index('static int n1x_boot_prepare_hfrp_once('):
                     source.index('static int n1x_boot_validate_firmware(')]
    start = source[source.index('static int n1x_boot_start_once('):
                   source.index('static struct attribute *boot_attrs[]')]
    harness = context_harness() + r'''
struct device {int id;};
static struct device dla_dev, sroot_dev;
static struct n1x_boot_endpoint dla = {.dev=&dla_dev, .hfrp=&dla_dev};
static struct n1x_boot_endpoint sroot = {.dev=&sroot_dev, .sroot=true};
enum fault {NONE, DLA_DMA, SROOT_DMA, POLICY, PM, STAMP_READ, STAMP_OLD,
 MODULE, HANDSHAKE, VERSION, VERSION_OLD, CONFIGURE, POWER};
static enum fault fault;
static unsigned dma_checks, policy_reads, pm_gets, pm_puts, pm_refs;
static unsigned module_gets, module_refs, handshakes, versions, configs, powers;
static unsigned uploads, accesses, stamp_reads;
static u32 stamp = 0x20251722;
#define THIS_MODULE ((void *)1)
#define WRITE_ONCE(x, v) ((x)=(v))
#define dev_info(...) ((void)0)
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep)
{dma_checks++;return ((ep==&dla && fault==DLA_DMA) ||
 (ep==&sroot && fault==SROOT_DMA)) ? -EOPNOTSUPP : 0;}
static int n1x_boot_live_board_policy(struct n1x_boot_context *b)
{assert(b==boot);policy_reads++;return fault==POLICY ? -EBADMSG : 0;}
static int pm_runtime_resume_and_get(struct device *d)
{assert(d==&dla_dev);pm_gets++;if(fault==PM)return -EIO;pm_refs++;return 0;}
static void pm_runtime_put(struct device *d)
{assert(d==&dla_dev && pm_refs);pm_puts++;pm_refs--;}
static bool try_module_get(void *module)
{assert(module==THIS_MODULE);module_gets++;if(fault==MODULE)return false;
 module_refs++;return true;}
static int n1x_boot_hfrp_write(void *context, u32 offset, u32 value)
{(void)context;(void)offset;(void)value;assert(0);return -EIO;}
static int n1x_boot_hfrp_read(void *context, u32 offset, u32 *value)
{assert(context==&dla && !offset && pm_refs==1);accesses++;stamp_reads++;
 if(fault==STAMP_READ)return -EIO;*value=fault==STAMP_OLD ? 0x20250211 : stamp;return 0;}
static void n1x_boot_delay(void *context, u32 us)
{(void)context;(void)us;assert(0);}
int n1x_hfrp_handshake(const struct n1x_dla_io *io, struct n1x_hfrp_channel *c,
 __u32 polls, __u32 interval, struct n1x_hfrp_exchange_result *r)
{assert(io->context==&dla && polls==5001 && interval==1000);
 assert(pm_refs==1 && module_refs==1 && boot->lifetime_held && boot->hfrp_held);
 assert(boot->hfrp.attempted && boot->hfrp.phase==1);handshakes++;
 memset(r,0,sizeof(*r));r->mode_select_attempted=r->mode_selected=r->reset_attempted=1;
 r->wait_offset=0x100;r->wait_value=1;r->wait_value_valid=1;r->wait_reads=5001;
 if(fault==HANDSHAKE){c->state=N1X_HFRP_FAILED;return -ETIMEDOUT;}
 r->reset_completed=r->peer_ready=r->ack_completed=r->reply_valid=1;
 r->reply.header=0x104;r->wait_offset=0x104;r->wait_value=2;r->wait_reads=1;
 c->state=N1X_HFRP_READY;return 0;}
int n1x_hfrp_discover_version(const struct n1x_dla_io *io, struct n1x_hfrp_channel *c,
 __u32 polls, __u32 interval, struct n1x_hfrp_version *v,
 struct n1x_hfrp_exchange_result *r)
{(void)io;(void)c;(void)polls;(void)interval;versions++;
 memset(r,0,sizeof(*r));memset(v,0,sizeof(*v));
 if(fault==VERSION)return -EIO;
 v->stamp=stamp;v->stamp_valid=v->valid=1;v->major=1;v->minor=fault==VERSION_OLD?3:5;return 0;}
int n1x_hfrp_configure_policy(const struct n1x_dla_io *io, struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v, const struct n1x_dla_vbios_policy *p,
 __u32 polls, __u32 interval, struct n1x_hfrp_setup_result *r)
{(void)io;(void)c;(void)v;(void)p;(void)polls;(void)interval;(void)r;
 configs++;return fault==CONFIGURE?-EREMOTEIO:0;}
int n1x_hfrp_power_on(const struct n1x_dla_io *io, struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v, __u8 pps, __u32 polls, __u32 interval,
 struct n1x_hfrp_exchange_result *r)
{(void)io;(void)c;(void)v;(void)pps;(void)polls;(void)interval;powers++;
 memset(r,0,sizeof(*r));r->reply.header=0xa06;
 return fault==POWER?-ETIMEDOUT:0;}
static int n1x_boot_upload(struct n1x_boot_context *b)
{assert(b==boot);uploads++;return 0;}
#define NEXT_STAGE(name) static int name(struct n1x_boot_context *b) \
 {assert(b==boot);return 0;}
NEXT_STAGE(n1x_boot_ping_once)
NEXT_STAGE(n1x_boot_init_region5_once)
NEXT_STAGE(n1x_boot_read_config_once)
NEXT_STAGE(n1x_boot_apply_config_once)
NEXT_STAGE(n1x_boot_apply_lpwr_once)
NEXT_STAGE(n1x_boot_bypass_task_once)
''' + prepare + start + r'''
static void reset(enum fault f)
{test_context_reset();fault=f;boot->dla=&dla;boot->sroot=&sroot;
 dma_checks=policy_reads=pm_gets=pm_puts=pm_refs=module_gets=module_refs=0;
 handshakes=versions=configs=powers=uploads=accesses=stamp_reads=0;}
int main(void)
{
 const char *stages[]={"ready","dla-dma","sroot-dma","vbios-policy","runtime-pm",
  "stamp","stamp","module-reference","handshake","version","version","policy","power-on"};
 for(enum fault f=NONE;f<=POWER;f++) {
  reset(f);int error=n1x_boot_start_once(boot);
  assert(!strcmp(boot->hfrp.stage,stages[f]) && boot->hfrp.error==error);
  assert((error==0)==(f==NONE));assert(uploads==(f==NONE));
  assert(pm_gets==(f==NONE || f>=PM));
  assert(stamp_reads==(f==NONE || f>=STAMP_READ));
  assert(boot->hfrp.initial_stamp_valid==(f==NONE || f>=STAMP_OLD));
  if(boot->hfrp.initial_stamp_valid)
   assert(boot->hfrp.initial_stamp==(f==STAMP_OLD?0x20250211:stamp));
  bool published=f==NONE || f>=HANDSHAKE;
  assert(boot->hfrp.attempted==published && boot->hfrp_held==published);
  assert(pm_refs==(unsigned)published && module_refs==(unsigned)published);
  assert(boot->lifetime_held==published);
  assert(pm_puts==(f==STAMP_READ || f==STAMP_OLD || f==MODULE));
  assert(boot->hfrp.powered==(f==NONE));
  if(f==HANDSHAKE) {
   assert(error==-ETIMEDOUT && !versions && !configs && !powers);
   assert(boot->hfrp.handshake.wait_value==1 && !boot->hfrp.handshake.reset_completed);
  }
  if(f==NONE || f>HANDSHAKE) {
   assert(boot->hfrp.handshake.reset_completed && boot->hfrp.handshake.peer_ready);
   assert(boot->hfrp.handshake.reply.header==0x104);
   assert(boot->hfrp.handshake.wait_value==2);
  }
  if(published) {
   struct n1x_boot_hfrp_state saved=boot->hfrp;
   unsigned before=accesses, held=boot->refs.value;
   assert(n1x_boot_start_once(boot)==-EALREADY);
   assert(n1x_boot_prepare_hfrp_once(boot)==-EALREADY);
   assert(!memcmp(&saved,&boot->hfrp,sizeof(saved)) && accesses==before);
   assert(boot->refs.value==held && pm_refs==1 && module_refs==1);
  }
 }
 reset(NONE);boot->sroot=NULL;
 assert(n1x_boot_prepare_hfrp_once(boot)==-ENODEV);
 assert(!strcmp(boot->hfrp.stage,"endpoints") && !accesses && !pm_refs && !module_refs);
 reset(STAMP_OLD);assert(n1x_boot_start_once(boot)==-EOPNOTSUPP);
 assert(boot->hfrp.initial_stamp_valid && !boot->hfrp.attempted);
 fault=STAMP_READ;assert(n1x_boot_start_once(boot)==-EIO);
 assert(!boot->hfrp.initial_stamp_valid && !boot->hfrp.initial_stamp);
 assert(!pm_refs && !module_refs && !handshakes);
 puts("HFRP startup: all preflight/transport failures, stamp/handshake retention, balanced pre-publication cleanup and retry guards: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-startup-') as tmp:
        path, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        path.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
            '-fsanitize=address,undefined', '-I', str(ROOT / 'include'),
            str(path), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
