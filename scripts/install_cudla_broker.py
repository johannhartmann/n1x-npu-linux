#!/usr/bin/env python3
"""Install the minimal broker, pinned models and libraries as root-owned files.

This does not load/reload a module, start firmware, trigger udev or alter boot.
The new device policy takes effect the next time the accel node is created.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = Path('/usr/lib/n1x-npu-linux')
BROKER = Path('/usr/libexec/n1x-dla-broker')
RULE = Path('/etc/udev/rules.d/99-n1x-dla.rules')
SYSUSERS = Path('/usr/lib/sysusers.d/n1x-npu-linux.conf')
FRONTEND_SHA256 = '89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9'
SUPERRES_SHA256 = 'c1ffde5d1eb9c7486a08ecc1470a1b633a11c7a71411f8fb36064d16dbcfc1c0'


def digest(data): return hashlib.sha256(data).hexdigest()


def checked(path, expected=None):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'missing or unsafe input: {path}')
    data = path.read_bytes()
    if expected is not None and digest(data) != expected:
        raise ValueError(f'hash mismatch: {path}')
    return data


def secure_directory(path):
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0 or status.st_mode & 0o022:
        raise PermissionError(f'installation directory is not root-owned and immutable: {path}')


def atomic_install(path, data, mode):
    secure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_uid != 0 or status.st_mode & 0o022 or path.read_bytes() != data:
        raise RuntimeError(f'installed file verification failed: {path}')


def atomic_symlink(path, target):
    secure_directory(path.parent)
    temporary = path.parent/('.'+path.name+'.tmp')
    if temporary.exists() or temporary.is_symlink(): temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, path)
    if not path.is_symlink() or os.readlink(path) != target:
        raise RuntimeError(f'installed link verification failed: {path}')


def models(selection):
    manifest = json.loads((ROOT/'config/vision-models.json').read_text())
    rows = []
    if selection in ('superres', 'all'):
        rows.append(('superres', ROOT/'results/native-compile-t254-superres/superres.nvda', SUPERRES_SHA256))
    if selection in ('vision', 'all'):
        for name, values in manifest['models'].items():
            rows.append((name, ROOT/'results'/('native-compile-t254-'+name)/(name+'.nvda'), values['sha256']))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', choices=('superres','vision','all'), default='superres',
                        help='root-owned compiled model set to install')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run once through pkexec after reviewing the built artifacts')
    frontend = ROOT/'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive/lib/libcudla.so.1.0.0'
    payloads = {
        PREFIX/'libn1x_dla.so': checked(ROOT/'runtime/libn1x_dla.so'),
        PREFIX/'cudla/libnvcudla.so': checked(ROOT/'runtime/cudla/libnvcudla.so'),
        PREFIX/'cudla/libcudla.so.1.0.0': checked(frontend, FRONTEND_SHA256),
        BROKER: checked(ROOT/'runtime/cudla/n1x-dla-broker'),
        RULE: checked(ROOT/'config/99-n1x-dla.rules'),
        SYSUSERS: checked(ROOT/'config/n1x-npu-linux.sysusers'),
    }
    model_payloads = [(name, checked(path, expected)) for name, path, expected in models(args.models)]
    for path, data in payloads.items(): atomic_install(path, data, 0o755 if path == BROKER else 0o644)
    subprocess.run(['/usr/bin/systemd-sysusers', str(SYSUSERS)], check=True)
    account = pwd.getpwnam('n1x-dla')
    if not account.pw_uid or not account.pw_gid:
        raise RuntimeError('dedicated n1x-dla account is unsafe')
    atomic_symlink(PREFIX/'cudla/libcudla.so.1', 'libcudla.so.1.0.0')
    for name, data in model_payloads: atomic_install(PREFIX/'models'/(name+'.nvda'), data, 0o644)
    receipt = dict(protocol=1, abi=2, frontend_sha256=FRONTEND_SHA256,
                   broker_sha256=digest(payloads[BROKER]), backend_sha256=digest(payloads[PREFIX/'cudla/libnvcudla.so']),
                   models={name:digest(data) for name,data in model_payloads},
                   device_policy='root:root 0600 on next accel-node creation',
                   broker_identity=dict(name='n1x-dla',uid=account.pw_uid,gid=account.pw_gid))
    atomic_install(PREFIX/'install-receipt.json', (json.dumps(receipt,indent=2,sort_keys=True)+'\n').encode(), 0o644)
    print('Installed root-owned N1X broker and pinned models; no device, firmware, boot or power action performed')


if __name__ == '__main__': main()
