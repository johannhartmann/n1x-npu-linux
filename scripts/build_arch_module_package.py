#!/usr/bin/env python3
"""Build a pinned, explicit-load Arch package from the committed NPU sources."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/arch'
OUTPUT = ROOT / 'results/arch-module-package'
SOURCES = ('driver', 'include', 'README.md')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, cwd=ROOT, **kwargs)


def main():
    for tree in ('driver', 'include'):
        run('git', 'diff', '--quiet', 'HEAD', '--', tree)
    revision = subprocess.check_output(
        ['git', 'rev-parse', '--short=8', 'HEAD'], cwd=ROOT, text=True).strip()
    if OUTPUT.exists():
        raise SystemExit(f'{OUTPUT} exists; move it aside before rebuilding')
    OUTPUT.mkdir(parents=True)
    archive = OUTPUT / 'npu-source.tar.gz'
    with archive.open('wb') as stream:
        run('git', 'archive', '--format=tar.gz', '--prefix=npu-source/', 'HEAD',
            *SOURCES, stdout=stream)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    template = (PACKAGING / 'PKGBUILD').read_text()
    (OUTPUT / 'PKGBUILD').write_text(template.replace('SOURCE_REV', revision)
        .replace('SOURCE_SHA256', digest))
    shutil.copy2(PACKAGING / 'n1x-npu-bootstrap.install', OUTPUT)
    subprocess.run(['makepkg', '--noconfirm'], cwd=OUTPUT, check=True)
    for package in sorted(OUTPUT.glob('*.pkg.tar.*')):
        print(package)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
