#!/usr/bin/env python3
"""Fault-check the actual kernel allocation function with mocked DMA boundaries."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_linux.c').read_text()
    start = source.index('static int n1x_test_dma_allocation(')
    end = source.index('static int n1x_linux_read32(', start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
#define DMA_BIT_MASK(n) ((1ULL << (n))-1)
#define GFP_KERNEL 0
#define dev_info(...) ((void)0)
struct acpi_device {struct {bool cca_seen,coherent_dma;} flags;};
struct iommu_domain {unsigned type;};
struct device {struct acpi_device *adev;u64 *dma_mask;bool coherent;};
static struct acpi_device adev;
static struct iommu_domain domain;
static u64 mask;
static struct device device;
static bool has_domain,backend,allocation_failure,damage;
static int mask_error,allocations,frees,cases;
static dma_addr_t allocation_address;
static unsigned char storage[70000];
static size_t allocated_size;
#define ACPI_COMPANION(d) ((d)->adev)
static struct iommu_domain *iommu_get_domain_for_dev(struct device *d)
{(void)d;return has_domain?&domain:NULL;}
static bool dev_is_dma_coherent(struct device *d) {return d->coherent;}
static bool iommu_is_dma_domain(struct iommu_domain *d) {return !!(d->type&2);}
static bool use_dma_iommu(struct device *d) {(void)d;return backend;}
static int dma_set_mask_and_coherent(struct device *d,u64 m)
{assert(d==&device);mask=m;return mask_error;}
static void *dma_alloc_coherent(struct device *d,size_t bytes,dma_addr_t *a,int flags)
{assert(d==&device && flags==GFP_KERNEL && bytes<=sizeof(storage));allocations++;
 allocated_size=bytes;*a=allocation_address;return allocation_failure?NULL:storage;}
static void dma_free_coherent(struct device *d,size_t bytes,void *b,dma_addr_t a)
{assert(d==&device && bytes==allocated_size && b==storage && a==allocation_address);frees++;}
static void *memchr_inv(void *b,int value,size_t bytes)
{unsigned char *p=b;if(damage)p[bytes-1]^=1;
 for(size_t i=0;i<bytes;i++){if(p[i]!=(unsigned char)value)return p+i;}
 return NULL;}
'''
    harness += source[start:end]
    harness += r'''
static void reset(bool sroot)
{adev.flags.cca_seen=true;adev.flags.coherent_dma=!sroot;
 device=(struct device){.adev=&adev,.dma_mask=&mask,.coherent=!sroot};
 domain.type=3;has_domain=backend=true;allocation_failure=damage=false;mask_error=0;
 allocation_address=sroot?0xe0000000ULL:0x100000000ULL;}
static void check(bool sroot,int expected,int a,int f)
{allocations=frees=0;assert(n1x_test_dma_allocation(&device,sroot)==expected);
 assert(allocations==a && frees==f);cases++;}
int main(void)
{
 reset(true);check(true,0,1,1);assert(allocated_size==67508 && mask==0xffffffffULL);
 reset(false);check(false,0,1,1);assert(allocated_size==4096 && mask==0xfffffffffULL);
 reset(true);adev.flags.cca_seen=false;check(true,-EOPNOTSUPP,0,0);
 reset(true);device.adev=NULL;check(true,-EOPNOTSUPP,0,0);
 reset(true);device.dma_mask=NULL;check(true,-EOPNOTSUPP,0,0);
 reset(true);adev.flags.coherent_dma=true;check(true,-EINVAL,0,0);
 reset(true);device.coherent=true;check(true,-EINVAL,0,0);
 reset(false);adev.flags.coherent_dma=false;check(false,-EINVAL,0,0);
 reset(false);device.coherent=false;check(false,-EINVAL,0,0);
 reset(true);has_domain=false;check(true,-EOPNOTSUPP,0,0);
 reset(true);domain.type=4;check(true,-EOPNOTSUPP,0,0);
 reset(true);domain.type=1;check(true,-EOPNOTSUPP,0,0);
 reset(true);backend=false;check(true,-EOPNOTSUPP,0,0);
 reset(true);mask_error=-EIO;check(true,-EIO,0,0);
 reset(true);allocation_failure=true;check(true,-ENOMEM,1,0);
 reset(true);allocation_address=0xdfffff00ULL;check(true,-ERANGE,1,1);
 reset(true);allocation_address=0x100000000ULL;check(true,-ERANGE,1,1);
 reset(true);allocation_address=(0x100000000ULL-67508)&~255ULL;check(true,0,1,1);
 allocation_address+=256;check(true,-ERANGE,1,1);
 reset(true);allocation_address++;check(true,-ERANGE,1,1);
 reset(false);allocation_address=0;check(false,-ERANGE,1,1);
 reset(false);allocation_address=1ULL<<36;check(false,-ERANGE,1,1);
 reset(true);damage=true;check(true,-EIO,1,1);
 printf("Actual kernel allocation function: %d gate/window/cleanup cases passed; DMA APIs mocked\n",cases);
 return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-linux-dma-') as tmp:
        path = Path(tmp)
        (path/'check.c').write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-Wall', '-Wextra', '-Werror', str(path/'check.c'), '-o', str(path/'check')], check=True)
        subprocess.run([str(path/'check')], check=True)


if __name__ == '__main__':
    main()
