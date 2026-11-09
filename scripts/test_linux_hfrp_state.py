#!/usr/bin/env python3
"""Unit tests for the sysfs HFRP power-query handlers with mocked OS/transport."""
import os
from pathlib import Path
from boot_test_context import context_harness
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    state = ""
    store = source[source.index('static ssize_t query_power_state_store('):
                   source.index('static DEVICE_ATTR_WO(query_power_state);')]
    show = source[source.index('static ssize_t hfrp_status_show('):
                  source.index('static ssize_t status_show(')]
    harness = context_harness() + r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#include "n1x_hfrp_setup.h"
typedef uint32_t u32;
struct device {int id;};
struct device_attribute {int unused;};

static struct device dla_dev,other_dev;
static struct n1x_boot_endpoint ep={.dev=&dla_dev,.hfrp=&dla_dev},sroot;

static bool privileged;
static int locks,queries,logs,emits,reply_error,cases;
#define CAP_SYS_RAWIO 17
static bool capable(int cap) {assert(cap==CAP_SYS_RAWIO);return privileged;}
static bool sysfs_streq(const char *a,const char *b)
{size_t n=strlen(b);return !strncmp(a,b,n) && (!a[n] || (a[n]=='\n' && !a[n+1]));}
static void mutex_lock(void *test_mutex) {int *lock=test_mutex;assert(lock==&boot_lock && !*lock);*lock=1;locks++;}
static void mutex_unlock(void *test_mutex) {int *lock=test_mutex;assert(lock==&boot_lock && *lock);*lock=0;}
static int n1x_boot_hfrp_write(void *ctx,u32 off,u32 value)
{(void)ctx;(void)off;(void)value;assert(0);return -EIO;}
static int n1x_boot_hfrp_read(void *ctx,u32 off,u32 *value)
{(void)ctx;(void)off;(void)value;assert(0);return -EIO;}
static void n1x_boot_delay(void *ctx,u32 us) {(void)ctx;(void)us;assert(0);}
#define dev_info(...) do {assert(boot_lock);logs++;} while(0)
/* Do not interpret Linux's %phN with libc printf. Check serialization and
 * that cached status emission never calls the transport instead.
 */
static int sysfs_emit(char *buf,const char *fmt,...)
{assert(boot_lock && strstr(fmt,"query_raw=%#x") && strstr(fmt,"query_error=%d"));
 emits++;strcpy(buf,"cached");return 6;}
''' + state + r'''
int n1x_hfrp_query_power_state(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v,u32 polls,u32 interval,struct n1x_hfrp_power_state *s,
 struct n1x_hfrp_exchange_result *r)
{assert(boot_lock && io->context==&ep && io->write32==n1x_boot_hfrp_write &&
 io->read32==n1x_boot_hfrp_read && io->delay_us==n1x_boot_delay);
 assert(c==&boot_hfrp.channel && v==&boot_hfrp.version && polls==5001 && interval==1000);
 assert(s==&boot_hfrp.power_state && r==&boot_hfrp.state_exchange && boot_hfrp.state_attempted);
 struct n1x_hfrp_exchange_result empty={0};
 assert(!s->valid && !s->raw && !memcmp(r,&empty,sizeof(empty)));queries++;
 if(reply_error==-ETIMEDOUT)c->state=N1X_HFRP_FAILED;
 if(!reply_error){s->raw=0xa5;s->valid=1;r->reply.header=0x1234;r->reply.length=1;r->reply.payload[0]=0xa5;}
 return reply_error;}
''' + store + show + r'''
static void reset(void)
{test_context_reset();boot_stopping=boot_sleep_prepared=false;memset(&boot_hfrp,0,sizeof(boot_hfrp));boot_dla=&ep;boot_sroot=&sroot;ep.hfrp=&dla_dev;
 privileged=true;locks=queries=logs=emits=reply_error=0;
 assert(!boot_lock);boot_hfrp.attempted=boot_hfrp.powered=true;boot_hfrp.phase=5;
 boot_hfrp.channel.state=N1X_HFRP_READY;boot_hfrp.exchange.reply.header=0x5678;
 boot_hfrp.state_attempted=true;boot_hfrp.state_error=-EIO;
 boot_hfrp.power_state.valid=1;boot_hfrp.power_state.raw=0xff;
 memset(&boot_hfrp.state_exchange,0x5a,sizeof(boot_hfrp.state_exchange));}
static void rejected(struct device *dev,const char *text,int error,int expected_locks)
{unsigned char before[sizeof(boot_hfrp)];memcpy(before,&boot_hfrp,sizeof(before));
 assert(query_power_state_store(dev,NULL,text,strlen(text))==error);
 assert(!boot_lock && locks==expected_locks && !queries && !logs);
 assert(!memcmp(before,&boot_hfrp,sizeof(before)));cases++;}
static void cached(void)
{char buf[32];int calls=queries;assert(hfrp_status_show(&dla_dev,NULL,buf)==6);
 assert(!strcmp(buf,"cached") && queries==calls && !boot_lock);
 assert(hfrp_status_show(&other_dev,NULL,buf)==-ENODEV && !boot_lock);cases++;}
int main(void)
{reset();privileged=false;rejected(&dla_dev,"query\n",-EPERM,0);
 reset();rejected(&dla_dev,"query extra\n",-EPERM,0);
 reset();boot_dla=NULL;rejected(&dla_dev,"query\n",-ENODEV,1);
 reset();rejected(&other_dev,"query\n",-ENODEV,1);
 reset();ep.hfrp=NULL;rejected(&dla_dev,"query\n",-ENODEV,1);
 reset();boot_sroot=NULL;rejected(&dla_dev,"query\n",-ENODEV,1);
 reset();boot_stopping=true;rejected(&dla_dev,"query\n",-ESHUTDOWN,1);
 reset();boot_sleep_prepared=true;rejected(&dla_dev,"query\n",-EBUSY,1);
 reset();boot_hfrp.attempted=false;rejected(&dla_dev,"query\n",-EHOSTDOWN,1);
 reset();boot_hfrp.powered=false;rejected(&dla_dev,"query\n",-EHOSTDOWN,1);
 reset();boot_hfrp.error=-EIO;rejected(&dla_dev,"query\n",-EHOSTDOWN,1);
 for(int state=N1X_HFRP_NEW;state<=N1X_HFRP_FAILED;state++){
 if(state==N1X_HFRP_READY)continue;reset();boot_hfrp.channel.state=state;
 rejected(&dla_dev,"query\n",state==N1X_HFRP_FAILED?-ENOTRECOVERABLE:-EBUSY,1);}
 reset();for(int i=1;i<=2;i++){
 assert(query_power_state_store(&dla_dev,NULL,"query\n",6)==6);
 assert(!boot_lock && queries==i && logs==i && !boot_hfrp.state_error);
 assert(boot_hfrp.power_state.valid && boot_hfrp.power_state.raw==0xa5);
 assert(boot_hfrp.state_exchange.reply.header==0x1234);cases++;cached();}
 const int errors[]={-EOPNOTSUPP,-EREMOTEIO,-ETIMEDOUT};
 for(unsigned i=0;i<sizeof(errors)/sizeof(errors[0]);i++){
 reset();reply_error=errors[i];assert(query_power_state_store(&dla_dev,NULL,"query",5)==reply_error);
 assert(!boot_lock && queries==1 && logs==1 && boot_hfrp.state_error==reply_error);
 assert(!boot_hfrp.power_state.valid && !boot_hfrp.power_state.raw);
 assert(boot_hfrp.attempted && boot_hfrp.powered && !boot_hfrp.error && boot_hfrp.phase==5);
 assert(boot_hfrp.exchange.reply.header==0x5678);cases++;cached();
 if(reply_error==-ETIMEDOUT){
 assert(query_power_state_store(&dla_dev,NULL,"query",5)==-ENOTRECOVERABLE);
 assert(!boot_lock && queries==1 && logs==1 && boot_hfrp.state_error==-ETIMEDOUT);cases++;}}
 printf("%d Linux HFRP diagnostic cases passed; mocked OS/transport, no hardware access\n",cases);}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-state-') as tmp:
        code, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
            '-Wno-misleading-indentation', '-I' + str(ROOT / 'include'),
            str(code), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
