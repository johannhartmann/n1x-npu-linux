// SPDX-License-Identifier: GPL-2.0-only
// One read of the exact locator used by the pinned Windows NPU fallback.
// Optional bounded ROM capture after the observed locator and iomem guards.
// No register writes, arbitrary addresses, retry or driver bind.
// The register's hardware name and accessibility are not inferred from its use.
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <sys/stat.h>
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

static void fault(int sig)
{
    const char message[] = "Locator read faulted; no ROM access attempted\n";
    (void)write(STDERR_FILENO, message, sizeof(message)-1);
    _exit(128+sig);
}

static int reserved_window(uint32_t base, size_t size)
{
    FILE *f = fopen("/proc/iomem", "r");
    char *line = NULL, label[128];
    size_t capacity = 0;
    unsigned long long start, end;
    int covered = 0, invalid = 0;
    if (!f) return 0;
    while (getline(&line, &capacity, f) >= 0) {
        if (sscanf(line, " %llx-%llx : %127[^\n]", &start, &end, label) != 3)
            continue;
        if (start < (uint64_t)base+size && base <= end) {
            if (strcmp(label, "reserved")) invalid = 1;
            if (start <= base && (uint64_t)base+size-1 <= end) covered = 1;
        }
    }
    free(line);
    fclose(f);
    return covered && !invalid;
}

int main(int argc, char **argv)
{
    struct utsname kernel;
    struct sigaction action = { .sa_handler = fault };
    if (argc != 1 && (argc != 3 || strcmp(argv[1], "--capture"))) {
        fputs("Usage: read_p16_vbios_locator [--capture NEW_FILE]\n", stderr);
        return 1;
    }
    if (uname(&kernel) || strcmp(kernel.release, "7.2.8-23-omarchy-p16-asus-npu2") ||
        !match("/sys/class/dmi/id/sys_vendor", "ASUS") ||
        !match("/sys/class/dmi/id/product_name", "ProArt P16 H7607BA") ||
        !match("/sys/class/dmi/id/bios_version", "H7607BA.300") ||
        sysconf(_SC_PAGESIZE) != 4096) {
        fputs("Machine/kernel differs from audited target\n", stderr);
        return 1;
    }
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGBUS, &action, NULL) || sigaction(SIGSEGV, &action, NULL)) {
        perror("sigaction"); return 1;
    }
    int fd = open("/dev/mem", O_RDONLY | O_SYNC | O_CLOEXEC);
    if (fd < 0) { perror("open /dev/mem"); return 1; }
    void *base = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, 0x1182b000);
    if (base == MAP_FAILED) { perror("mmap locator"); close(fd); return 1; }
    uint32_t locator = *(volatile const uint32_t *)((const char *)base + 0xc80);
    munmap(base, 4096);
    uint32_t candidate = (locator << 12) + UINT32_C(0x80000600);
    if (argc == 3) {
        const size_t length = 0x110000;
        const size_t within_page = candidate & 4095;
        if (candidate != UINT32_C(0xb9600600) || !reserved_window(candidate, length)) {
            fputs("ROM locator/reserved window differs from observed P16 BIOS 300\n", stderr);
            close(fd); return 1;
        }
        void *mapping = mmap(NULL, length+within_page, PROT_READ, MAP_SHARED,
                             fd, candidate & ~UINT32_C(4095));
        if (mapping == MAP_FAILED) { perror("mmap ROM"); close(fd); return 1; }
        const unsigned char *rom = (const unsigned char *)mapping+within_page;
        // The Windows parser searches the whole bounded window and walks
        // backwards from BIT to an aligned AA55 header. It does not require
        // the first byte of this window to be the start of a PCI image.
        unsigned char *copy = malloc(length);
        if (!copy) { perror("malloc"); munmap(mapping, length+within_page); close(fd); return 1; }
        memcpy(copy, rom, length);
        if (memcmp(copy, rom, length)) {
            fputs("ROM changed during capture\n", stderr);
            free(copy); munmap(mapping, length+within_page); close(fd); return 1;
        }
        munmap(mapping, length+within_page);
        int out = open(argv[2], O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC, 0600);
        if (out < 0) { perror("create output"); free(copy); close(fd); return 1; }
        const char *uid = getenv("PKEXEC_UID");
        if (uid && fchown(out, (uid_t)strtoul(uid, NULL, 10), (gid_t)-1)) {
            perror("chown output"); close(out); free(copy); close(fd); return 1;
        }
        size_t position = 0;
        while (position < length) {
            ssize_t n = write(out, copy+position, length-position);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { perror("write output"); close(out); free(copy); close(fd); return 1; }
            position += (size_t)n;
        }
        free(copy);
        if (close(out)) { perror("close output"); close(fd); return 1; }
    }
    close(fd);
    printf("{\"locator_register\":\"0x1182bc80\",\"locator\":\"0x%08" PRIx32
           "\",\"candidate_rom_pa\":\"0x%08" PRIx32
           "\",\"rom_accessed\":%s}\n", locator, candidate, argc==3 ? "true" : "false");
    return 0;
}
