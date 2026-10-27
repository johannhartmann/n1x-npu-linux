#!/usr/bin/env python3
"""Check real ReLU preparation/oracle code with mocked Linux/device boundaries.

Uses the actual pinned template and independent CPU reference. The shared
completion loop is fault-tested separately by test_linux_bypass_task.py.
"""
import os
import hashlib
from pathlib import Path
import shlex
import subprocess
import struct
import tempfile
from emulate_profile1_commands import run, HOST, MMIO, ACK, PROFILE1_HASH

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    functions = source[source.index('static int n1x_boot_validate_relu_firmware('):
                       source.index('static ssize_t relu_probe_store(')]
    state = source[source.index('struct n1x_boot_task_state {'):
                   source.index('static bool n1x_boot_machine(')]
    constants = '\n'.join(line for line in source.splitlines()
                          if line.startswith('#define N1X_RELU_'))
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <openssl/sha.h>
#include "n1x_dla_task.h"
#include "n1x_dla_response.h"
#include "n1x_dla_vbios.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef __u64 u64;
typedef uint64_t dma_addr_t;
typedef uint32_t __le32;
#define SHA256_DIGEST_SIZE 32
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0
#define WRITE_ONCE(x,v) ((x)=(v))
#define cpu_to_le32(x) (x)
#define dev_info(...) ((void)0)
struct device {int unused;};
struct n1x_boot_endpoint {struct device *dev;};
static struct device device;
static struct n1x_boot_endpoint endpoint={&device};
static struct n1x_boot_endpoint *boot_sroot,*boot_dla;
static struct {bool accepted;} boot_lpwr;
static struct {bool powered;struct n1x_dla_vbios_policy policy;} boot_hfrp;
struct firmware {size_t size;const u8 *data;};
static struct firmware firmware;
static bool enable_relu_probe,machine_ok,alloc_fail,poison,corrupt_input;
static int gate_error,read_error,request_error,mask_error,run_error;
static int allocations,frees,releases,runs,barriers,cases;
static u32 pending;
static dma_addr_t allocation_address;
'''+constants+'\n'+state+r'''
static u8 image[N1X_RELU_BYTES],allocation[N1X_RELU_BYTES],prepared[N1X_RELU_BYTES],oracle[N1X_RELU_IO_BYTES];
static bool n1x_boot_machine(void) {return machine_ok;}
static int n1x_boot_dma_gate(struct n1x_boot_endpoint *ep) {assert(ep==&endpoint);return gate_error;}
static int n1x_boot_read(void *ctx,u32 offset,u32 *value)
{assert(ctx==&endpoint && offset==8);*value=pending;return read_error;}
/* Shared helper is exercised with real transport in test_linux_bypass_task.py. */
static int n1x_boot_drain_task_idle(struct n1x_boot_task_state *task)
{int err=n1x_boot_read(&endpoint,8,&task->pending);if(err)return err;
 return task->pending&0xc0 ? -EBUSY : 0;}
static int request_firmware_direct(const struct firmware **out,const char *name,struct device *dev)
{assert(dev==&device && !strcmp(name,N1X_RELU_FIRMWARE));if(request_error)return request_error;
 *out=&firmware;return 0;}
static void release_firmware(const struct firmware *f) {assert(f==&firmware);releases++;}
static void sha256(const u8 *data,size_t bytes,u8 *out) {assert(SHA256(data,bytes,out)==out);}
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{assert(dev==&device && mask==DMA_BIT_MASK(36));return mask_error;}
static void *dma_alloc_coherent(struct device *dev,size_t bytes,dma_addr_t *dma,int flags)
{assert(dev==&device && bytes==N1X_RELU_BYTES && flags==GFP_KERNEL);allocations++;
 *dma=allocation_address;memset(allocation,0xa5,sizeof(allocation));return alloc_fail?NULL:allocation;}
static void dma_free_coherent(struct device *dev,size_t bytes,void *buffer,dma_addr_t dma)
{assert(dev==&device && bytes==N1X_RELU_BYTES && buffer==allocation && dma==allocation_address);
 assert(!boot_relu.attempted);frees++;}
static void dma_rmb(void) {barriers++;}
static u64 word64(const u8 *p) {u64 x=0;for(unsigned i=0;i<8;i++)x|=(u64)p[i]<<(8*i);return x;}
static int n1x_boot_run_task(struct n1x_boot_task_state *task)
{assert(task==&boot_relu && releases==1 && !task->attempted && !frees);runs++;
 assert(!memcmp(allocation+1024,image+1024,sizeof(image)-1024));
 assert(allocation[41]==0); /* arithmetic execution, not BYPASS_EXEC */
 assert(word64(allocation+21)==allocation_address+256);
 assert(word64(allocation+65)==allocation_address+768);
 assert(word64(allocation+256)==allocation_address+16384); /* network first */
 assert(word64(allocation+264)==allocation_address+8192); /* input */
 assert(word64(allocation+272)==allocation_address+12288); /* output */
 assert(word64(allocation+312)==allocation_address+2830336); /* final mapped extent */
 memcpy(prepared,allocation,sizeof(prepared));
 task->attempted=true;
 if(run_error)return run_error;
 task->accepted=task->signal_seen=task->verified=true;task->fence=0x12345678;
 if(!poison)memcpy(allocation+N1X_RELU_OUTPUT,oracle,sizeof(oracle));
 if(corrupt_input)allocation[N1X_RELU_INPUT]^=1;
 return 0;}
'''+functions+r'''
static void reset(void)
{boot_dla=boot_sroot=&endpoint;boot_task.verified=boot_lpwr.accepted=true;
 boot_hfrp.powered=true;boot_hfrp.policy.boot_tables_valid=1;machine_ok=enable_relu_probe=true;
 memset(&boot_relu,0,sizeof(boot_relu));boot_relu_numeric=false;
 memset(boot_relu_input_hash,0,32);memset(boot_relu_output_hash,0,32);
 alloc_fail=poison=corrupt_input=false;pending=0;allocation_address=HOST_ADDRESS;
 gate_error=read_error=request_error=mask_error=run_error=0;
 allocations=frees=releases=runs=barriers=0;firmware=(struct firmware){sizeof(image),image};}
static void check(int error,int allocated,int freed,int released,bool attempted,bool numeric)
{assert(n1x_boot_relu_once()==error);assert(allocations==allocated&&frees==freed&&releases==released);
 assert(boot_relu.attempted==attempted&&boot_relu_numeric==numeric&&runs==(int)attempted);
 assert(!!boot_relu.buffer==attempted);
 if(attempted){assert(n1x_boot_relu_once()==-EALREADY);assert(runs==1&&allocations==1&&frees==0);}
 cases++;}
static void read_file(const char *path,u8 *out,size_t size)
{FILE *f=fopen(path,"rb");assert(f);assert(fread(out,1,size,f)==size&&fgetc(f)==EOF);fclose(f);}
int main(int argc,char **argv)
{assert(argc==4);read_file(argv[1],image,sizeof(image));read_file(argv[2],oracle,sizeof(oracle));
 reset();enable_relu_probe=false;check(-EPERM,0,0,0,false,false);
 reset();machine_ok=false;check(-EPERM,0,0,0,false,false);
 reset();boot_dla=NULL;check(-ENODEV,0,0,0,false,false);
 reset();boot_sroot=NULL;check(-ENODEV,0,0,0,false,false);
 reset();boot_task.verified=false;check(-ENODEV,0,0,0,false,false);
 reset();boot_lpwr.accepted=false;check(-ENODEV,0,0,0,false,false);
 reset();boot_hfrp.powered=false;check(-ENODEV,0,0,0,false,false);
 reset();boot_hfrp.policy.boot_tables_valid=0;check(-ENODEV,0,0,0,false,false);
 reset();gate_error=-EIO;check(-EIO,0,0,0,false,false);
 reset();read_error=-EIO;check(-EIO,0,0,0,false,false);
 reset();pending=0x40;check(-EBUSY,0,0,0,false,false);
 reset();pending=0x80;check(-EBUSY,0,0,0,false,false);
 reset();request_error=-ENOENT;check(-ENOENT,0,0,0,false,false);
 reset();firmware.size--;check(-EBADMSG,0,0,1,false,false);
 reset();firmware.data=NULL;check(-EBADMSG,0,0,1,false,false);
 reset();image[1234]^=1;check(-EBADMSG,0,0,1,false,false);image[1234]^=1;
 reset();mask_error=-EIO;check(-EIO,0,0,1,false,false);
 reset();alloc_fail=true;check(-ENOMEM,1,0,1,false,false);
 for(int i=0;i<4;i++){reset();allocation_address=i==0?0:i==1?0x100002100ULL:
 i==2?DMA_BIT_MASK(36)+1:DMA_BIT_MASK(36)-4095;check(-ERANGE,1,1,1,false,false);}
 reset();run_error=-ETIMEDOUT;check(-ETIMEDOUT,1,0,1,true,false);assert(!barriers);
 reset();run_error=-EREMOTEIO;check(-EREMOTEIO,1,0,1,true,false);assert(!barriers);
 reset();poison=true;check(-EBADMSG,1,0,1,true,false);assert(boot_relu.verified&&barriers==1);
 reset();corrupt_input=true;check(-EBADMSG,1,0,1,true,false);assert(boot_relu.verified&&barriers==1);
 reset();check(0,1,0,1,true,true);assert(boot_relu.verified&&barriers==1);
 FILE *f=fopen(argv[3],"wb");assert(f);assert(fwrite(prepared,1,sizeof(prepared),f)==sizeof(prepared));assert(!fclose(f));
 printf("%d actual ReLU preparation/lifetime/oracle cases passed; no hardware access\n",cases);}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-relu-linux-') as tmp:
        code, binary, prepared = Path(tmp)/'test.c', Path(tmp)/'test', Path(tmp)/'prepared.bin'
        code.write_text('#define HOST_ADDRESS '+hex(HOST)+'ULL\n'+harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_task.c'), '-lcrypto', '-o', str(binary)], check=True)
        subprocess.run([str(binary), str(ROOT/'results/p16-relu-template.bin'),
                        str(ROOT/'results/relu-probe/expected-chw16-fp16.bin'), str(prepared)], check=True)
        payload = bytearray(prepared.read_bytes())
    # Feed the exact kernel-prepared bytes to the original raw firmware's
    # reset/trap/DMA/scheduler path. Engine arithmetic is still not emulated.
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    def response(u):
        for offset in (8, 0xd28):
            value = struct.unpack('<I', u.mem_read(MMIO+offset, 4))[0]
            u.mem_write(MMIO+offset, struct.pack('<I', value & ~128))
        u.mem_write(MMIO+0x40, struct.pack('<I', ACK))
    result = run(firmware, method=0x307, host_payload=payload, host_handler=response,
                 boot_from_reset=True, via_trap=True, dma_register_model=True, resume_scheduler=True)
    assert result['returned'] and result['scheduler_wfi']
    assert not result['function_stubs'] and result['firmware_code_unmodified']
    assert result['interrupts'] == [(0x702, HOST >> 8)] and not result['task_interrupts']
    offsets = (16384, 8192, 12288, 20480, 53248, 57344, 2797568)
    for index, offset in enumerate(offsets):
        assert (0x2505c+8*index, 4, HOST+offset) in result['scheduler_mmio_writes']
        assert (0x25060+8*index, 4, 0) in result['scheduler_mmio_writes']
    print('Exact Linux-prepared task accepted by unmodified raw HAL-1 scheduler; engine arithmetic remains unmodeled')


if __name__ == '__main__':
    main()
