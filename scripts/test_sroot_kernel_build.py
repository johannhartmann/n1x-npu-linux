#!/usr/bin/env python3
"""Check build preconditions reject unsafe destinations/mismatched bases."""
from pathlib import Path
import subprocess
import platform
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-build-guards-') as tmp:
        root = Path(tmp)
        source = root/'source'
        source.mkdir()
        (source/'.config').write_text('deliberately not the running configuration\n')
        destination = root/'output'
        cases = [([str(source), str(destination), '--jobs', '0'], 'jobs must be'),
                 ([str(source), str(source/'inside')], 'outside the known-working'),
                 ([str(source), str(source)], 'outside the known-working'),
                 ([str(source), str(destination)], 'does not match the running kernel')]
        for args, diagnostic in cases:
            result = subprocess.run([sys.executable, str(ROOT/'scripts/build_sroot_kernel.py'), *args],
                                    capture_output=True, text=True)
            assert result.returncode == 2 and diagnostic in result.stderr, result.stderr
            assert not destination.exists() and not (source/'inside').exists()
        (source/'.config').write_bytes((Path('/usr/lib/modules')/platform.release()/'build/.config').read_bytes())
        (source/'arch/arm64/boot').mkdir(parents=True)
        (source/'arch/arm64/boot/Image').write_bytes(b'deliberately not the running Image')
        result = subprocess.run([sys.executable, str(ROOT/'scripts/build_sroot_kernel.py'),
                                 str(source), str(destination)], capture_output=True, text=True)
        assert result.returncode == 2 and 'Image does not match' in result.stderr
        assert not destination.exists()
        destination.mkdir()
        marker = destination/'untouched'
        marker.write_text('existing build')
        result = subprocess.run([sys.executable, str(ROOT/'scripts/build_sroot_kernel.py'),
                                 str(source), str(destination)], capture_output=True, text=True)
        assert result.returncode == 2 and 'destination must be new' in result.stderr
        assert marker.read_text() == 'existing build'
    print('Six kernel-build guards passed; no source copy/build/install performed')


if __name__ == '__main__':
    main()
