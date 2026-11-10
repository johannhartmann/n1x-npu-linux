#!/usr/bin/env python3
"""Check real VBIOS cache/reset code without display MMIO or firmware access."""
from pathlib import Path
import subprocess
import tempfile

from boot_test_context import context_layout

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    functions = source[source.index('static int n1x_boot_board_policy('):
                       source.index('/* Separate, bounded HFRP resource.')]
    reset = source[source.index('static void n1x_boot_reset_state('):
                   source.index('static int n1x_sroot_component_bind(')]
    code = context_layout() + r'''
#define N1X_VBIOS_CARVEOUT_SIZE 4096
#define N1X_VBIOS_CARVEOUT_BASE 0x100000
#define N1X_DISP_VBIOS_LOCATOR_PHYS 0x1182bc80
#define IORESOURCE_SYSTEM_RAM 1
#define IORES_DESC_NONE 0
#define REGION_DISJOINT 0
#define MEMREMAP_WB 1
#define WRITE_ONCE(x,v) ((x)=(v))
static unsigned fault,accesses,maps,unmaps;
static char rom[N1X_VBIOS_CARVEOUT_SIZE];
static u32 locator=1;
static void *ioremap(resource_size_t address,size_t bytes)
{assert(address==N1X_DISP_VBIOS_LOCATOR_PHYS && bytes==4);accesses++;return fault==1?NULL:&locator;}
static u32 readl(void *p){assert(p==&locator);accesses++;return locator;}
static void iounmap(void *p){assert(p==&locator);accesses++;}
static int region_intersects(resource_size_t p,size_t size,int flags,int desc)
{assert(p==0x101000 && size==sizeof(rom) && flags==1 && !desc);accesses++;return fault==2?1:0;}
static void *memremap(resource_size_t p,size_t size,int flags)
{assert(p==0x101000 && size==sizeof(rom) && flags==1);accesses++;if(fault==3)return NULL;maps++;return rom;}
static void memunmap(void *p){assert(p==rom);accesses++;unmaps++;}
int n1x_dla_parse_vbios_policy(const __u8 *data,size_t bytes,struct n1x_dla_vbios_policy *p,
 struct n1x_dla_vbios_location *l)
{assert(data==(void *)rom && bytes==sizeof(rom));accesses++;if(fault==4)return -EBADMSG;
 *p=(struct n1x_dla_vbios_policy){.boot_tables_valid=1,.frequency_khz=800000};
 *l=(struct n1x_dla_vbios_location){.bit_offset=99};return 0;}
int n1x_dla_verify_vbios_pcir(const __u8 *data,size_t bytes,const struct n1x_dla_vbios_location *l)
{assert(data==(void *)rom && bytes==sizeof(rom) && l->bit_offset==99);accesses++;return fault==5?-EINVAL:0;}
int n1x_dla_prepare_gating(const struct n1x_dla_vbios_policy *p,struct n1x_dla_gating_policy *g)
{assert(p->frequency_khz==800000);accesses++;if(fault==6)return -EINVAL;
 *g=(struct n1x_dla_gating_policy){.request_mask=0x777};return 0;}
''' + functions + reset + r'''
int main(void)
{
 struct n1x_boot_context b={.sleep_prepared=true};
 assert(n1x_boot_live_board_policy(&b)==-EHOSTDOWN && !accesses);
 b.sleep_prepared=false;
 for(fault=1;fault<=6;fault++) {
  assert(n1x_boot_live_board_policy(&b)<0 && !b.board_policy.valid);
  assert(maps==unmaps);
 }
 fault=0;assert(!n1x_boot_live_board_policy(&b) && b.board_policy.valid && maps==unmaps);
 unsigned saved=accesses;
 struct n1x_boot_board_policy cached=b.board_policy;
 for(unsigned i=0;i<32;i++) {
  b.shutdown.hardware_off=b.shutdown.released=true;
  b.hfrp.attempted=b.hfrp.powered=true;
  n1x_boot_reset_state(&b);
  assert(!b.hfrp.attempted && !b.hfrp.powered && !b.hfrp.policy.boot_tables_valid);
  assert(!memcmp(&cached,&b.board_policy,sizeof(cached)));
  b.sleep_prepared=true;fault=1;
  assert(!n1x_boot_live_board_policy(&b) && accesses==saved);
  assert(b.hfrp.policy.frequency_khz==800000 && b.hfrp.gating.request_mask==0x777);
  assert(b.hfrp.location.bit_offset==99 && !b.hfrp.powered && !b.hfrp.attempted);
 }
 puts("Board policy cache: six validation/map faults and 32 MMIO-free resume cycles: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-board-policy-cache-') as tmp:
        path, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        path.write_text(code)
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                        '-I', str(ROOT / 'include'), str(path), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
