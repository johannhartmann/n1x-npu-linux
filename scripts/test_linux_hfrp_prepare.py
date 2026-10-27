#!/usr/bin/env python3
"""Fault-test the actual Linux HFRP prelude with mocked OS/transport edges.

Real captured ROM, SHA-256 and native policy decoder; no hardware access.
The separate channel/setup tests cover packet bytes and MMIO fault handling.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    state = source[source.index('static struct {\n\tstruct n1x_hfrp_channel'):
                   source.index('} boot_hfrp;')+len('} boot_hfrp;')]
    functions = source[source.index('static bool n1x_boot_hfrp_allowed('):
                       source.index('static int n1x_boot_validate_firmware(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <openssl/sha.h>
#include "n1x_hfrp_setup.h"
typedef uint8_t u8;
typedef uint32_t u32;
#define SHA256_DIGEST_SIZE 32
#define N1X_BOARD_FIRMWARE "nvidia/n1x/p16-bios300-physical.rom"
#define THIS_MODULE ((void *)0x1234)
#define dev_info(...) ((void)0)
struct device {int id;};
struct firmware {size_t size;const u8 *data;};
struct n1x_boot_endpoint {struct device *dev;void *hfrp;};
static struct device dla_dev={0},sroot_dev={1};
static u32 registers[128];
static struct n1x_boot_endpoint dla_ep={&dla_dev,registers},sroot_ep={&sroot_dev,NULL};
static struct n1x_boot_endpoint *boot_dla,*boot_sroot;
static bool enable_hfrp,boot_attempted,machine_ok,module_ok;
static int gates[2],request_error,resume_error,resume_refs,module_refs,releases,requests;
static int step,fail_step,reads,writes,cases;
static u8 image[0x110000];
static struct firmware fw;
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {return gates[ep->dev->id];}
static int request_firmware_direct(const struct firmware **out,const char *name,struct device *dev)
{assert(dev==&dla_dev && !strcmp(name,N1X_BOARD_FIRMWARE));requests++;
 if(request_error)return request_error;*out=&fw;return 0;}
static void release_firmware(const struct firmware *f) {assert(f==&fw);releases++;}
static void sha256(const u8 *data,size_t size,u8 *out) {assert(SHA256(data,size,out)==out);}
static int pm_runtime_resume_and_get(struct device *dev)
{assert(dev==&dla_dev);if(resume_error)return resume_error;resume_refs++;return 0;}
static void pm_runtime_put(struct device *dev) {assert(dev==&dla_dev && resume_refs);resume_refs--;}
static bool try_module_get(void *module) {assert(module==THIS_MODULE);if(!module_ok)return false;module_refs++;return true;}
static u32 readl(void *ptr) {assert(ptr>= (void *)registers && ptr<(void *)(registers+128));reads++;return *(u32 *)ptr;}
static void writel(u32 value,void *ptr)
{assert(ptr>=(void *)registers && ptr<(void *)(registers+128));writes++;*(u32 *)ptr=value;}
static void n1x_boot_delay(void *ctx,u32 us) {(void)ctx;(void)us;}
'''+state+r'''
static int enter(int expected)
{assert(++step==expected && boot_hfrp.attempted && resume_refs==1 && module_refs==1);
 return step==fail_step ? -EIO : 0;}
int n1x_hfrp_handshake(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 u32 polls,u32 interval,struct n1x_hfrp_exchange_result *r)
{assert(c==&boot_hfrp.channel && polls==5001 && interval==1000 && r==&boot_hfrp.exchange);
 assert(!io->write32(io->context,4,1));return enter(1);}
int n1x_hfrp_discover_version(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 u32 polls,u32 interval,struct n1x_hfrp_version *v,struct n1x_hfrp_exchange_result *r)
{(void)io;(void)c;(void)polls;(void)interval;(void)r;v->valid=1;v->major=1;v->minor=5;return enter(2);}
int n1x_hfrp_configure_policy(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v,const struct n1x_dla_vbios_policy *p,u32 polls,u32 interval,
 struct n1x_hfrp_setup_result *r)
{(void)io;(void)c;(void)v;(void)polls;(void)interval;(void)r;
 assert(p->frequency_khz==430000 && p->boot_tables_valid==1);return enter(3);}
int n1x_hfrp_power_on(const struct n1x_dla_io *io,struct n1x_hfrp_channel *c,
 const struct n1x_hfrp_version *v,u8 pps,u32 polls,u32 interval,struct n1x_hfrp_exchange_result *r)
{(void)io;(void)c;(void)v;(void)polls;(void)interval;(void)r;assert(pps==1);return enter(4);}
'''+functions+r'''
static void reset(void)
{memset(&boot_hfrp,0,sizeof(boot_hfrp));memset(registers,0,sizeof(registers));registers[0]=0x20251722;
 boot_dla=&dla_ep;boot_sroot=&sroot_ep;dla_ep.hfrp=registers;enable_hfrp=machine_ok=module_ok=true;
 boot_attempted=false;gates[0]=gates[1]=request_error=resume_error=resume_refs=module_refs=0;
 releases=requests=step=fail_step=reads=writes=0;fw=(struct firmware){sizeof(image),image};}
static void early(int expected)
{assert(n1x_boot_prepare_hfrp_once()==expected);assert(!boot_hfrp.attempted && !boot_hfrp.powered);
 assert(!resume_refs && !module_refs && !writes && !step);cases++;}
int main(int argc,char **argv)
{assert(argc==2);FILE *f=fopen(argv[1],"rb");assert(f);
 assert(fread(image,1,sizeof(image),f)==sizeof(image) && fgetc(f)==EOF);fclose(f);
 reset();enable_hfrp=false;early(-EPERM);
 reset();machine_ok=false;early(-EPERM);
 reset();boot_dla=NULL;early(-ENODEV);
 reset();boot_sroot=NULL;early(-ENODEV);
 reset();dla_ep.hfrp=NULL;early(-ENODEV);
 reset();boot_attempted=true;early(-EALREADY);
 reset();gates[0]=-EOPNOTSUPP;early(-EOPNOTSUPP);
 reset();gates[1]=-EOPNOTSUPP;early(-EOPNOTSUPP);
 reset();request_error=-ENOENT;early(-ENOENT);assert(requests==1 && releases==0);
 reset();fw.size--;early(-EBADMSG);assert(releases==1);
 reset();fw.data=NULL;early(-EBADMSG);assert(releases==1);
 reset();image[100]^=1;early(-EBADMSG);assert(releases==1);image[100]^=1;
 reset();resume_error=-EIO;early(-EIO);assert(releases==1);
 reset();registers[0]++;early(-EOPNOTSUPP);
 reset();module_ok=false;early(-ENODEV);
 for(int i=0;i<=4;i++){reset();fail_step=i;
 assert(n1x_boot_prepare_hfrp_once()==(i?-EIO:0));
 assert(boot_hfrp.attempted && boot_hfrp.powered==!i && boot_hfrp.phase==(u32)(i?i:5));
 assert(resume_refs==1 && module_refs==1 && releases==1 && writes==1 && step==(i?i:4));
 assert(n1x_boot_prepare_hfrp_once()==-EALREADY && writes==1 && step==(i?i:4));cases++;}
 reset();u32 value;assert(n1x_boot_hfrp_read(&dla_ep,0,&value)==0 && value==0x20251722);
 for(u32 offset=0;offset<0x1000;offset++){
 bool aligned=!(offset&3);
 bool rd=aligned&&(offset==0||offset==0x100||offset==0x104||(offset>=0x110&&offset<=0x1fc));
 bool wr=aligned&&(offset==4||offset==0x100||offset==0x10c||(offset>=0x110&&offset<=0x184));
 assert(n1x_boot_hfrp_allowed(offset,false)==rd && n1x_boot_hfrp_allowed(offset,true)==wr);
 if(!rd)assert(n1x_boot_hfrp_read(&dla_ep,offset,&value)==-EINVAL);
 if(!wr)assert(n1x_boot_hfrp_write(&dla_ep,offset,0)==-EINVAL);
 }
 printf("%d prelude lifecycle cases and 4096 MMIO offset checks passed; no hardware access\n",cases);}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-linux-') as tmp:
        code, binary = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
            '-I'+str(ROOT/'include'), str(code), str(ROOT/'driver/n1x_dla_vbios.c'),
            '-lcrypto', '-o', str(binary)], check=True)
        subprocess.run([str(binary), str(ROOT/'results/p16-bios300-physical.rom')], check=True)


if __name__ == '__main__':
    main()
