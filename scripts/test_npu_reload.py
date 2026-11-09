#!/usr/bin/env python3
"""Verify operator shutdown/unload ordering and aborts with modeled OS calls."""
import errno
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

import qualify_npu_reload as q


def service_stop_tests():
    helper = (q.ROOT / 'packaging/systemd/n1x-npu-stop').read_text()
    for mode in ('not-loaded', 'missing', 'multiple', 'stop-error', 'busy', 'ok'):
        with tempfile.TemporaryDirectory(prefix='n1x-service-stop-') as tmp:
            root = Path(tmp)
            module = root / 'module/n1x_npu_bootstrap'
            driver = root / 'drivers/n1x-npu-bootstrap'
            log = root / 'unloaded'
            endpoint = driver / 'NVDA200A:07/shutdown'
            if mode != 'not-loaded':
                module.mkdir(parents=True)
            if mode not in ('not-loaded', 'missing'):
                endpoint.parent.mkdir(parents=True)
                if mode == 'stop-error':
                    endpoint.mkdir()
                else:
                    endpoint.write_text('')
                if mode == 'multiple':
                    second = driver / 'NVDA200A:08/shutdown'
                    second.parent.mkdir()
                    second.write_text('')
            modprobe = root / 'modprobe'
            modprobe.write_text('#!/bin/bash\n'
                + (f'exit 1\n' if mode == 'busy' else
                   f'test "$(cat {shlex.quote(str(endpoint))})" = stop || exit 3\n'
                   f'touch {shlex.quote(str(log))}\n'))
            modprobe.chmod(0o755)
            script = root / 'stop'
            script.write_text(helper.replace('/sys/module/', str(root / 'module') + '/')
                .replace('/sys/bus/platform/drivers/', str(root / 'drivers') + '/')
                .replace('/usr/bin/modprobe', shlex.quote(str(modprobe))))
            result = subprocess.run(['bash', str(script)], capture_output=True, text=True)
            assert (result.returncode == 0) == (mode in ('not-loaded', 'ok')), mode
            assert log.exists() == (mode == 'ok'), mode
    print('Service stop: missing/ambiguous endpoints, stop failure and busy unload fail safely: PASS')


def main():
    for mode in ('ok', 'stop-error', 'bad-state', 'retained', 'exec-accepted', 'busy'):
        with tempfile.TemporaryDirectory(prefix='n1x-reload-control-') as tmp:
            root = Path(tmp)
            module, dev, sroot, node = (root/n for n in ('module', 'dla', 'sroot', 'accel0'))
            for path in (module, dev, sroot):
                path.mkdir()
            for path in (dev/'driver', sroot/'driver', node):
                path.touch()
            (module/'refcnt').write_text('2')
            events = []

            def trigger(path, payload):
                assert path == dev/'shutdown' and payload == b'stop\n'
                events.append('stop')
                if mode == 'stop-error':
                    raise OSError(errno.ETIMEDOUT, 'uncertain stop')
                (dev/'shutdown_status').write_text('error=0 hardware_off=1 released='+
                                                    ('0' if mode == 'bad-state' else '1')+
                                                    ' hfrp_held=0 upload_held=0')

            class Device:
                def __init__(self, path):
                    assert path == str(node)

                def __enter__(self):
                    events.append('open')
                    (module/'refcnt').write_text('1')
                    return self

                def info(self):
                    return dict(retained_jobs=0, retained_bytes=4096 if mode == 'retained' else 0)

                def submit(self, *args):
                    events.append('blocked-exec')
                    if mode != 'exec-accepted':
                        raise OSError(errno.ENODEV, 'stopped')

                def __exit__(self, *args):
                    events.append('close')
                    (module/'refcnt').write_text('2' if mode == 'busy' else '0')

            def run(argv, **kw):
                assert argv == ['rmmod', 'n1x_npu_bootstrap'] and events[-1] in ('blocked-exec', 'close')
                if int((module/'refcnt').read_text()):
                    assert not kw.get('check')
                    events.append('busy-unload')
                    return SimpleNamespace(returncode=1)
                assert kw.get('check') and events[-1] == 'close'
                events.append('unload')
                (module/'refcnt').unlink()
                module.rmdir()
                (dev/'driver').unlink()
                (sroot/'driver').unlink()
                node.unlink()
                return SimpleNamespace(returncode=0)

            with patch.multiple(q, MODULE=module, ENDPOINT=dev, SROOT=sroot, DEBUGFS=dev, NODE=node,
                                Device=Device, trigger=trigger), \
                 patch.object(q.os, 'sync'), patch.object(q.subprocess, 'run', side_effect=run):
                failed = False
                try:
                    q.stop_and_unload(lambda **row: None)
                except (OSError, RuntimeError):
                    failed = True
                assert failed == (mode != 'ok'), (mode, events)
            assert events.count('stop') == 1
            if mode == 'ok':
                assert events == ['stop', 'open', 'blocked-exec', 'busy-unload', 'close', 'unload']
                assert not module.exists()
            else:
                assert module.exists() and 'unload' not in events
    print('6 reload-controller success/abort/ownership cases passed; no root or hardware')
    service_stop_tests()


if __name__ == '__main__':
    main()
