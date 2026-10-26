/* SPDX-License-Identifier: GPL-2.0-only
 * Bounded userspace /dev/kmsg recorder for the diagnostic initramfs.
 * It cannot observe a kernel which never reaches userspace.
 */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define LIMIT (8 * 1024 * 1024)
static volatile sig_atomic_t stop;
static void stopped(int signal_number) { (void)signal_number; stop = 1; }
static time_t now(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return ts.tv_sec;
}
static int put(int fd, const void *data, size_t size)
{
    const char *p = data;
    while (size) {
        ssize_t n = write(fd, p, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        size -= n;
    }
    return 0;
}
int main(int argc, char **argv)
{
    char buffer[8192], *end;
    long seconds;
    if (argc != 3) return 2;
    errno = 0;
    seconds = strtol(argv[2], &end, 10);
    if (errno || !*argv[2] || *end || seconds < 1 || seconds > 600) return 2;
    int input = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (input < 0) { perror("n1x-bootlog: /dev/kmsg"); return 1; }
    int output = open(argv[1], O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output < 0) { perror("n1x-bootlog: output"); close(input); return 1; }
    char *parent = strdup(argv[1]);
    if (!parent) { close(output); close(input); return 1; }
    char *slash = strrchr(parent, '/');
    if (slash == parent) slash[1] = 0;
    else if (slash) *slash = 0;
    int directory = open(slash ? parent : ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    free(parent);
    if (directory < 0) { close(output); close(input); return 1; }
    signal(SIGTERM, stopped);
    signal(SIGINT, stopped);
    time_t start = now(), synced = start;
    size_t total = 0;
    int result = 0;
    const char header[] = "N1X_BOOTLOG_START: initramfs/userspace recorder; kernel backlog follows\n";
    if (start < 0 || put(output, header, sizeof(header) - 1) || fdatasync(output)) result = 1;
    if (fsync(directory)) result = 1;
    close(directory);
    while (!result && !stop && total < LIMIT && now() - start < seconds) {
        struct pollfd pfd = {.fd = input, .events = POLLIN};
        int ready = poll(&pfd, 1, 1000);
        if (ready < 0 && errno != EINTR) { result = 1; break; }
        if (ready > 0) {
            ssize_t n = read(input, buffer, sizeof(buffer));
            if (n > 0) {
                size_t length = (size_t)n < LIMIT - total ? (size_t)n : LIMIT - total;
                if (put(output, buffer, length)) result = 1;
                total += length;
            } else if (n < 0 && errno == EPIPE) {
                const char lost[] = "N1X_BOOTLOG_WARNING: kernel ring buffer overrun\n";
                size_t length = sizeof(lost) - 1 < LIMIT - total ? sizeof(lost) - 1 : LIMIT - total;
                if (put(output, lost, length)) result = 1;
                total += length;
            } else if (!n || (errno != EAGAIN && errno != EINTR)) {
                result = 1;
            }
        }
        if (now() != synced) {
            if (fdatasync(output)) result = 1;
            synced = now();
        }
    }
    int n = snprintf(buffer, sizeof(buffer), "\nN1X_BOOTLOG_END: bytes=%zu error=%d stopped=%d\n",
                     total, result, (int)stop);
    if (put(output, buffer, n) || fdatasync(output)) result = 1;
    close(output);
    close(input);
    return result;
}
