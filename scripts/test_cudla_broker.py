#!/usr/bin/env python3
"""Exercise broker authentication and framing without root, CUDA or a device."""
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
import cudla_broker


class Sink:
    def __init__(self): self.data = bytearray(); self.closed = False
    def write(self, data): self.data.extend(data); return len(data)
    def flush(self): pass
    def close(self): self.closed = True


class Process:
    def __init__(self, output):
        self.stdin, self.stdout, self.output = Sink(), io.BytesIO(output), None
    def poll(self): return self.output
    def wait(self, timeout=None): self.output = 0; return 0


class Model:
    def __init__(self, _):
        def tensor(name, size):
            return SimpleNamespace(name=name, bytes=size, shape=(1, 1, 1, 1),
                                   format_codes=(3, 2, 0, 0, 0),
                                   strides=(2, 2, 2, 2, 2, 0, 0, 0))
        self.inputs, self.outputs = [tensor('input', 4)], [tensor('output', 6)]


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-broker-test-') as temporary:
        path = Path(temporary)/'model.nvda'; path.write_bytes(b'tested broker model')
        digest = hashlib.sha256(path.read_bytes()).digest()
        info = cudla_broker.INFO.pack(2, 15, 32 << 20, 512, 4096, 64 << 20, 1, 128, 0)
        stream = b''.join((
            cudla_broker.HEADER.pack(b'N1XB', 1, 1, cudla_broker.MODEL_INFO.size),
            cudla_broker.MODEL_INFO.pack(4, 6, digest),
            cudla_broker.HEADER.pack(b'N1XR', 1, cudla_broker.INFO_COMMAND, len(info)), info,
            cudla_broker.HEADER.pack(b'N1XR', 1, 0, 6), b'result'))
        process = Process(stream)
        with patch.object(cudla_broker, 'BROKER', Path('/usr/bin/true')), \
             patch.object(cudla_broker, 'Model', Model), \
             patch.object(cudla_broker.os, 'geteuid', return_value=1000), \
             patch.object(cudla_broker.subprocess, 'Popen', return_value=process) as opened:
            with cudla_broker.Device({'fixture': path}) as device:
                assert device.info()['abi_version'] == 2
                assert device.load('fixture').packed(b'abcd') == b'result'
            assert opened.call_args.args[0] == ['pkexec', '/usr/bin/true', 'fixture']
            expected = b''.join((
                cudla_broker.HEADER.pack(b'N1XQ', 1, cudla_broker.INFO_COMMAND, 0),
                cudla_broker.HEADER.pack(b'N1XQ', 1, 0, 4), b'abcd',
                cudla_broker.HEADER.pack(b'N1XQ', 1, cudla_broker.QUIT_COMMAND, 0)))
            assert process.stdin.data == expected and process.stdin.closed
        link = Path(temporary)/'broker'; link.symlink_to('/usr/bin/true')
        with patch.object(cudla_broker, 'BROKER', link), patch.object(cudla_broker, 'Model', Model):
            try: cudla_broker.Device({'fixture': path})
            except RuntimeError as error: assert 'regular file' in str(error)
            else: raise AssertionError('symlinked broker accepted')
    print('Broker ownership, model digest, framing, INFO, inference and quit checks passed; no root/CUDA/device access')


if __name__ == '__main__': main()
