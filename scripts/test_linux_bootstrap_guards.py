#!/usr/bin/env python3
"""Compile exact bootstrap machine/DMA/MMIO guards against explicit mock state."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    guards = source[source.index('static bool n1x_boot_machine('):
                    source.index('static int n1x_boot_read(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_registers.h"
typedef uint32_t u32;
#define N1X_BOOT_KERNEL "7.2.8-23-omarchy-p16-asus-npu1"
struct {struct {char release[80];} name;} init_uts_ns;
enum {DMI_SYS_VENDOR,DMI_PRODUCT_NAME,DMI_BIOS_VERSION};
static const char *dmi_values[3];
static bool dmi_match(int key,const char *expected) {return !strcmp(dmi_values[key],expected);}
struct acpi_device {struct {bool cca_seen,coherent_dma;} flags;};
struct device {struct acpi_device *adev;uint64_t *dma_mask;bool coherent;};
struct n1x_boot_endpoint {struct device *dev;size_t bytes;u32 first;bool sroot;};
struct iommu_domain {unsigned type;};
struct iommu_fwspec {unsigned num_ids;u32 ids[2];};
struct iommu_group {int unused;};
static struct acpi_device adev;
static struct device device,foreign;
static struct iommu_domain domain;
static struct iommu_fwspec spec;
static struct iommu_group group;
static uint64_t mask;
static bool domain_present,spec_present,group_present,dma_backend,shared;
static int group_gets,group_puts,cases;
#define ACPI_COMPANION(d) ((d)->adev)
static struct iommu_domain *iommu_get_domain_for_dev(struct device *dev)
{assert(dev==&device);return domain_present?&domain:NULL;}
static struct iommu_fwspec *dev_iommu_fwspec_get(struct device *dev)
{assert(dev==&device);return spec_present?&spec:NULL;}
static bool dev_is_dma_coherent(struct device *dev) {return dev->coherent;}
static bool iommu_is_dma_domain(struct iommu_domain *dom) {return !!(dom->type&2);}
static bool use_dma_iommu(struct device *dev) {assert(dev==&device);return dma_backend;}
static struct iommu_group *iommu_group_get(struct device *dev)
{assert(dev==&device);if(!group_present)return NULL;group_gets++;return &group;}
static int iommu_group_for_each_dev(struct iommu_group *g,void *data,
                                   int (*callback)(struct device *,void *))
{assert(g==&group);int ret=callback(&device,data);if(ret)return ret;
 return shared?callback(&foreign,data):0;}
static void iommu_group_put(struct iommu_group *g) {assert(g==&group);group_puts++;}
'''
    harness += guards
    harness += r'''
static struct n1x_boot_endpoint reset(bool root)
{adev.flags.cca_seen=true;adev.flags.coherent_dma=!root;
 device=(struct device){&adev,&mask,!root};domain.type=3;
 spec=(struct iommu_fwspec){1,{root?1:0x2b,0}};
 domain_present=spec_present=group_present=dma_backend=true;shared=false;group_gets=group_puts=0;
 return (struct n1x_boot_endpoint){&device,root?180:0x40000,root?16:0,root};}
static void check(struct n1x_boot_endpoint *ep,int expected)
{assert(n1x_boot_dma_gate(ep)==expected);assert(group_gets==group_puts);cases++;}
int main(void)
{strcpy(init_uts_ns.name.release,N1X_BOOT_KERNEL);
 dmi_values[0]="ASUS";dmi_values[1]="ProArt P16 H7607BA";dmi_values[2]="H7607BA.300";
 assert(n1x_boot_machine());strcpy(init_uts_ns.name.release,"7.2.8-23-omarchy-p16-asus");
 assert(!n1x_boot_machine());strcpy(init_uts_ns.name.release,N1X_BOOT_KERNEL);
 for(int i=0;i<3;i++){const char *saved=dmi_values[i];dmi_values[i]="other";
 assert(!n1x_boot_machine());dmi_values[i]=saved;}
 for(int root=0;root<2;root++){
 struct n1x_boot_endpoint ep=reset(root);check(&ep,0);
 ep=reset(root);domain.type=11;check(&ep,0);
 ep=reset(root);device.adev=NULL;check(&ep,-EOPNOTSUPP);
 ep=reset(root);adev.flags.cca_seen=false;check(&ep,-EOPNOTSUPP);
 ep=reset(root);adev.flags.coherent_dma=root;check(&ep,-EOPNOTSUPP);
 ep=reset(root);device.coherent=root;check(&ep,-EOPNOTSUPP);
 ep=reset(root);device.dma_mask=NULL;check(&ep,-EOPNOTSUPP);
 ep=reset(root);domain_present=false;check(&ep,-EOPNOTSUPP);
 ep=reset(root);domain.type=4;check(&ep,-EOPNOTSUPP);
 ep=reset(root);dma_backend=false;check(&ep,-EOPNOTSUPP);
 ep=reset(root);spec_present=false;check(&ep,-EOPNOTSUPP);
 ep=reset(root);spec.num_ids=0;check(&ep,-EOPNOTSUPP);
 ep=reset(root);spec.num_ids=2;check(&ep,-EOPNOTSUPP);
 ep=reset(root);spec.ids[0]^=1;check(&ep,-EOPNOTSUPP);
 ep=reset(root);group_present=false;check(&ep,-EOPNOTSUPP);
 ep=reset(root);shared=true;check(&ep,-EINVAL);
 }
 struct n1x_boot_endpoint ep=reset(true);u32 offset;
 for(u32 logical=0;logical<256;logical++)for(int write=0;write<2;write++){
 unsigned index=logical/16;bool allowed=!(logical%16) && (write?
 (index==1||index==3||index==6||index==9||index==12):
 (index==2||index==4||index==5||index==7||index==8||index==11));
 offset=0xa5a5a5a5;int ret=n1x_boot_offset(&ep,logical,write,&offset);
 assert((ret==0)==allowed);if(allowed)assert(offset==logical-16);cases++;}
 ep=reset(true);ep.bytes=179;assert(n1x_boot_offset(&ep,192,true,&offset)==-ERANGE);
 ep=reset(false);assert(!n1x_boot_offset(&ep,0x4c,false,&offset) && offset==0x204c);
 assert(n1x_boot_offset(&ep,0x4c,true,&offset)==-EINVAL);
 assert(n1x_boot_offset(&ep,0xb88,false,&offset)==-EINVAL);
 for(u32 logical=0;logical<0x1000;logical+=4)for(int write=0;write<2;write++){
 bool allowed=write?(logical==0xd24||logical==0x6c||logical==0x68||logical==4||logical==0x40):
 (logical==0x4c||logical==0xd2c||logical==0xd28||logical==0x68||logical==8||logical==0x40||logical==0x44);
 assert((n1x_boot_offset(&ep,logical,write,&offset)==0)==allowed);cases++;}
 ep.bytes=0x204f;assert(n1x_boot_offset(&ep,0x4c,false,&offset)==-ERANGE);
 printf("%d DMA/MMIO guard cases plus machine/kernel and resource-end checks passed; no hardware access\n",cases);
 return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-bootstrap-guards-') as tmp:
        code, executable = Path(tmp)/'test.c', Path(tmp)/'test'
        code.write_text(harness)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            str(code), str(ROOT/'driver/n1x_dla_registers.c'), '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    main()
