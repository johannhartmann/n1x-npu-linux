// SPDX-License-Identifier: GPL-2.0-only
// Read-only snapshot of the already powered, owned DLA mailbox. No ACK/reset.
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <unistd.h>
static int match(const char *path, const char *expected)
{
    char value[128];
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    int ok = fgets(value, sizeof(value), f) != NULL;
    fclose(f);
    if (!ok) return 0;
    value[strcspn(value, "\n")] = 0;
    return strcmp(value, expected) == 0;
}


int main(void)
{
    struct utsname kernel;
    char status[2048];
    if (uname(&kernel) || strcmp(kernel.release, "7.2.8-23-rtx-spark-npu") ||
        !match("/sys/class/dmi/id/sys_vendor", "ASUS") ||
        !match("/sys/class/dmi/id/product_name", "ProArt P16 H7607BA") ||
        !match("/sys/class/dmi/id/bios_version", "H7607BA.300") ||
        !match("/sys/module/n1x_npu_bootstrap/srcversion", "E936E960ECE1CFAEFA88E1D") ||
        sysconf(_SC_PAGESIZE) != 4096)
        return 1;
    FILE *f = fopen("/sys/bus/platform/devices/NVDA200A:00/hfrp_status", "r");
    if (!f) { perror("hfrp_status"); return 1; }
    int ok = fgets(status, sizeof(status), f) != NULL;
    fclose(f);
    if (!ok || !strstr(status, "power_accepted=1 ") || !strstr(status, "phase=5 "))
        return 1;
    int fd = open("/dev/mem", O_RDONLY|O_SYNC|O_CLOEXEC);
    if (fd < 0) { perror("/dev/mem"); return 1; }
    void *base = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, 0x19002000);
    if (base == MAP_FAILED) { perror("mmap DLA"); close(fd); return 1; }
    volatile const uint32_t *p = base;
    uint32_t before = p[8/4], word = p[0x40/4], data = p[0x44/4], after = p[8/4];
    munmap(base, 4096);
    close(fd);
    printf("{\"irq_before\":\"0x%08x\",\"word\":\"0x%08x\",\"data\":\"0x%08x\",\"irq_after\":\"0x%08x\",\"message\":%u,\"command\":%u,\"status\":%u,\"upper\":%u,\"writes\":0}\n",
           before,word,data,after,word&255,(word>>8)&255,(word>>16)&255,word>>24);
    return before == after ? 0 : 2;
}
