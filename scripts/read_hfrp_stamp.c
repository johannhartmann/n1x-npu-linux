/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed P16 diagnostic: read HFRP stamp/mode only. No device/register writes,
 * reset, binding, runtime-PM transition or module operation. Run explicitly
 * as root; /dev/mem protections are respected, never disabled.
 */
#define _POSIX_C_SOURCE 200809L
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <unistd.h>

#ifndef __aarch64__
#error "This fixed hardware diagnostic is for AArch64 only"
#endif

static int matches(const char *path, const char *expected)
{
    char buffer[160];
    FILE *file = fopen(path, "r");
    int ok = file && fgets(buffer, sizeof(buffer), file) &&
             !strcmp(buffer, expected);
    if (file)
        fclose(file);
    return ok;
}

int main(void)
{
    char owner[256], line[256];
    struct utsname kernel;
    FILE *iomem;
    volatile const uint32_t *registers;
    uint32_t stamp, mode;
    long page_size = sysconf(_SC_PAGESIZE);
    int fd, resource = 0;
    ssize_t count;
    void *mapping;

    if (geteuid() || uname(&kernel) ||
        strcmp(kernel.release, "7.2.8-23-omarchy-p16-asus-npu1") ||
        !matches("/sys/class/dmi/id/product_name", "ProArt P16 H7607BA\n") ||
        !matches("/sys/class/dmi/id/bios_version", "H7607BA.300\n") ||
        !matches("/sys/bus/platform/devices/NVDA200A:00/power/runtime_status", "active\n") ||
        page_size <= 0 || 0x31380000UL % (unsigned long)page_size) {
        fputs("Refusing: root, exact kernel/platform/BIOS and active DLA required\n", stderr);
        return 1;
    }
    count = readlink("/sys/bus/platform/devices/NVDA200A:00/driver", owner, sizeof(owner)-1);
    if (count < 0)
        return 1;
    owner[count] = 0;
    if (strcmp(owner, "../../../bus/platform/drivers/n1x-npu-bootstrap")) {
        fputs("Refusing: expected existing bootstrap owner\n", stderr);
        return 1;
    }
    iomem = fopen("/proc/iomem", "r");
    if (!iomem)
        return 1;
    while (fgets(line, sizeof(line), iomem)) {
        if (!strcmp(line, "31380000-3138ffff : NVDA200A:00\n"))
            resource = 1;
    }
    fclose(iomem);
    if (!resource) {
        fputs("Refusing: exact ACPI HFRP resource not found\n", stderr);
        return 1;
    }
    fd = open("/dev/mem", O_RDONLY | O_SYNC | O_CLOEXEC);
    if (fd < 0) {
        perror("open /dev/mem");
        return 1;
    }
    mapping = mmap(NULL, (size_t)page_size, PROT_READ, MAP_SHARED, fd, 0x31380000);
    if (mapping == MAP_FAILED) {
        perror("mmap HFRP");
        close(fd);
        return 1;
    }
    registers = mapping;
    __asm__ volatile("dsb sy" ::: "memory");
    stamp = registers[0];
    mode = registers[1];
    __asm__ volatile("dsb sy" ::: "memory");
    munmap(mapping, (size_t)page_size);
    close(fd);
    printf("{\"physical_base\":\"0x31380000\",\"stamp\":\"0x%08x\","
           "\"mode\":\"0x%08x\",\"register_reads\":2,\"register_writes\":0}\n", stamp, mode);
    return 0;
}
