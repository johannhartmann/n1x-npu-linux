#!/usr/bin/env python3
"""Check physical-qualifier orchestration with pinned models and modeled OS I/O.

Requires local compiled model fixtures. Never opens CUDA or an NPU device.
"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

import qualify_cudla as q


def main():
    read_text = Path.read_text
    for mode in ('single', 'reuse', 'no-capability', 'growth', 'mismatch', 'process-error'):
        with tempfile.TemporaryDirectory(prefix='n1x-qualifier-') as tmp:
            out = Path(tmp)/'evidence'
            calls = []
            retained = dict(retained_bytes=0, retained_jobs=0)

            def cached(path, *args, **kwargs):
                if str(path) == '/sys/module/n1x_npu_bootstrap/srcversion':
                    return 'reviewed'
                if str(path) == '/sys/firmware/acpi/platform_profile':
                    return 'quiet'
                if path.parent == Path('/sys/bus/platform/devices/NVDA200A:00'):
                    return 'task_verified=1 lpwr_accepted=1 power_accepted=1'
                return read_text(path, *args, **kwargs)

            class Device:
                def __init__(self, path):
                    assert path == '/dev/accel/accel0'
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def info(self):
                    return dict(capabilities=0 if mode == 'no-capability' else 15,
                                retain_limit=64 << 20, max_jobs=128, **retained)

            def run(argv, **kwargs):
                assert argv[0] == str(q.ROOT/'runtime/cudla/cudla-smoke')
                assert kwargs['env']['N1X_DLA_DEVICE'] == '/dev/accel/accel0'
                count = (len(argv)-2)//3
                assert count == (1 if mode == 'single' else (2 if not calls else 3))
                calls.append(argv)
                jobs = json.loads((out/'plan.json').read_text())['jobs']
                by_input = {job['input']: job for job in jobs}
                assert len({Path(argv[i]).read_bytes() for i in range(2, len(argv), 3)}) == count
                retained['retained_jobs'] += 2 if mode == 'growth' else 1
                retained['retained_bytes'] += by_input[argv[2]]['retained_bytes']
                for i in range(2, len(argv), 3):
                    Path(argv[i+2]).write_bytes(b'bad' if mode == 'mismatch' else Path(argv[i+1]).read_bytes())
                return SimpleNamespace(returncode=int(mode == 'process-error'), stdout=b'modeled', stderr=b'')

            argv = ['qualify_cudla.py', '--output-dir', str(out)]
            if mode != 'single':
                argv.append('--reuse')
            with patch.object(q.sys, 'argv', argv), patch.object(q.os, 'geteuid', return_value=0), \
                 patch.object(q.platform, 'release', return_value=q.KERNEL), \
                 patch.object(q.subprocess, 'check_output', return_value='reviewed'), \
                 patch.object(q.subprocess, 'run', side_effect=run), \
                 patch.object(Path, 'read_text', cached), patch.object(q, 'Device', Device), \
                 contextlib.redirect_stdout(io.StringIO()):
                try:
                    q.main()
                except RuntimeError:
                    assert mode not in ('single', 'reuse'), mode
                else:
                    assert mode in ('single', 'reuse'), mode
            if mode in ('single', 'reuse'):
                complete = json.loads((out/'complete.json').read_text())
                assert complete['numerical_jobs'] == 8
                assert complete['reuse_submissions'] == (5 if mode == 'reuse' else 0)
                assert len(calls) == (3 if mode == 'reuse' else 8)
                assert json.loads((out/'preflight.json').read_text())['platform_profile'] == 'quiet'
            else:
                assert len(calls) == (0 if mode == 'no-capability' else 1)
                assert not (out/'complete.json').exists()
    print('6 cuDLA qualifier sequence/capability/allocation/error cases passed; no CUDA or NPU access')


if __name__ == '__main__':
    main()
