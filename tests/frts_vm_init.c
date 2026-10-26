/* VM-only init: compare the exported snapshot against a synthetic fixture. */
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <unistd.h>

int main(void)
{
    unsigned char data[0x9000], expected[0x9000] = {0};
    ssize_t total = 0, count;
    mkdir("/dev", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    int console = open("/dev/console", O_RDWR);
    if (console >= 0) {
        dup2(console, STDOUT_FILENO);
        dup2(console, STDERR_FILENO);
    }
    mkdir("/sys", 0755);
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    memcpy(expected, "GFWI", 4);
    expected[32] = 40;
    expected[36] = 64;
    for (int i = 0; i < 64; i++)
        expected[40 + i] = i;
    int fd = open("/sys/firmware/n1x_frts_snapshot", O_RDONLY);
    if (fd < 0)
        total = -1;
    else
        while (total < (ssize_t)sizeof(data) &&
               (count = read(fd, data + total, sizeof(data) - total)) > 0)
            total += count;
    puts(total == sizeof(data) && !memcmp(data, expected, sizeof(data)) ?
         "FRTS_VM_CAPTURE_PASS" : "FRTS_VM_CAPTURE_FAIL");
    fflush(stdout);
    reboot(RB_POWER_OFF);
    return 1;
}
