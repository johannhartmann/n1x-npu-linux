#!/usr/bin/env python3
"""Execute the snapshot C guards with mocked RAM/EFI/sysfs, never hardware.

The real kernel separately compiles the same source with its actual headers.
This harness checks that rejected inputs cause no RAM read or publication.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define __init
#define __initdata
#define __ro_after_init
#define early_param(...)
#define late_initcall(...)
#define pr_info(...) ((void)0)
#define EFI_BOOT 1
#define MEMBLOCK_NONE 0
struct memblock_region { uint64_t base, size; unsigned flags; } region_mock;
#define for_each_mem_region(r) for ((r) = &region_mock; (r); (r) = NULL)
struct bin_attribute {
    struct {const char *name; unsigned mode;} attr;
    unsigned size;
    int (*read)(void);
    void *private;
};
static int sysfs_bin_attr_simple_read(void) {return 0;}
static void *firmware_kobj;
static unsigned maps, unmaps, published, copied;
static bool efi, reserved, map_failed, mutate;
static u8 ram[0x9000];
static bool efi_enabled(int n) {assert(n == EFI_BOOT); return efi;}
static bool memblock_is_region_reserved(uint64_t a, uint64_t n)
{assert(a == 0xfc000000 && n == sizeof(ram)); return reserved;}
static void *early_memremap_ro(uint64_t a, unsigned n)
{assert(a == 0xfc000000 && n == sizeof(ram)); maps++; return map_failed ? NULL : ram;}
static void early_memunmap(void *a, unsigned n)
{assert(a == ram && n == sizeof(ram)); unmaps++;}
static u32 get_unaligned_le32(const u8 *p)
{return (u32)p[0] | (u32)p[1]<<8 | (u32)p[2]<<16 | (u32)p[3]<<24;}
static void memzero_explicit(void *p, unsigned n) {memset(p, 0, n);}
static int sysfs_create_bin_file(void *k, const struct bin_attribute *a)
{assert(k == firmware_kobj && a->attr.mode == 0400 && a->size == sizeof(ram));
 assert(!strcmp(a->attr.name, "n1x_frts_snapshot")); published++; return 0;}
static void *copy_mock(void *d, const void *s, size_t n)
{void *ret = memcpy(d,s,n); copied += n;
 if (mutate && s == ram && n == sizeof(ram)) ram[100] ^= 1;
 return ret;}
#define memcpy copy_mock
'''
TESTS = r'''
#undef memcpy
static void reset(void)
{
    requested = captured = false;
    maps = unmaps = published = copied = 0;
    efi = true; reserved = map_failed = mutate = false;
    region_mock = (struct memblock_region){0xfa002000, 0x05cfe000, 0};
    memset(snapshot, 0, sizeof(snapshot)); memset(ram, 0, sizeof(ram));
    memcpy(ram, "GFWI", 4); ram[32] = 40; ram[36] = 64;
}
static void check(bool ok, unsigned expected_maps)
{
    n1x_frts_snapshot_early();
    assert(captured == ok && maps == expected_maps);
    assert(n1x_frts_snapshot_publish() == 0 && published == (unsigned)ok);
    if (!expected_maps) assert(copied == 0);
    assert(unmaps == (map_failed ? 0 : expected_maps));
}
int main(void)
{
    reset(); check(false,0);
    reset(); assert(n1x_frts_snapshot_param(NULL) == -EINVAL); check(false,0);
    reset(); assert(n1x_frts_snapshot_param("1") == -EINVAL); check(false,0);
    reset(); assert(n1x_frts_snapshot_param("p16-bios300") == 0); check(true,1);
    assert(!memcmp(snapshot,ram,sizeof(ram)) && frts_attr.private == snapshot);
    reset(); requested=true; efi=false; check(false,0);
    reset(); requested=true; reserved=true; check(false,0);
    reset(); requested=true; region_mock.flags=4; check(false,0);
    reset(); requested=true; region_mock.size=0x1000; check(false,0);
    reset(); requested=true; region_mock.base=0xfd000000; check(false,0);
    reset(); requested=true; map_failed=true; check(false,1);
    reset(); requested=true; ram[0]=0; check(false,1); assert(copied==40);
    reset(); requested=true; ram[32]=39; check(false,1); assert(copied==40);
    reset(); requested=true; ram[36]=0; check(false,1); assert(copied==40);
    reset(); requested=true; ram[32]=0; ram[33]=0x90; check(false,1);
    reset(); requested=true; ram[37]=0x90; check(false,1);
    reset(); requested=true; mutate=true; check(false,1);
    for(unsigned i=0;i<sizeof(snapshot);i++) assert(snapshot[i]==0);
    puts("16 early-capture guard cases passed; EFI/RAM/sysfs mocked");
}
'''


def main():
    source = (ROOT / "driver/n1x_frts_snapshot.c").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
    with tempfile.TemporaryDirectory(prefix="frts-guards-") as directory:
        directory = Path(directory)
        (directory / "test.c").write_text(PRELUDE + source + TESTS)
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", str(directory / "test.c"),
                        "-o", str(directory / "test")], check=True)
        subprocess.run([str(directory / "test")], check=True)


if __name__ == "__main__":
    main()
