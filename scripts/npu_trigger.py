#!/usr/bin/env python3
"""Issue an explicit sysfs trigger without buffered close/flush resubmission.

The kernel's operation guards remain authoritative. No retries or short-write
completion loops are performed here; errors require inspecting retained state.
"""
import argparse
import errno
import os
from pathlib import Path

TRIGGERS = {'prepare_hfrp': b'prepare\n', 'upload': b'upload\n', 'ping': b'ping\n', 'init_region5': b'init\n',
            'read_config': b'read\n', 'apply_config': b'apply\n',
            'apply_lpwr': b'apply\n', 'bypass_task': b'run\n', 'relu_probe': b'run\n'}


def trigger(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(errno.EIO, 'short trigger write; not retried')
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=TRIGGERS)
    args = parser.parse_args()
    trigger(Path('/sys/bus/platform/devices/NVDA200A:00')/args.phase, TRIGGERS[args.phase])
