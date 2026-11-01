"""Unprivileged client for the installed, capability-confined N1X broker."""
import hashlib
import os
from pathlib import Path
import stat
import struct
import subprocess

from cudla_python import Descriptor, FRONTEND_SHA256, pack_tensor, unpack_tensor
from loadable import Model

BROKER = Path('/usr/libexec/n1x-dla-broker')
PROTOCOL = 1
HEADER = struct.Struct('<4sHHQ')
MODEL_INFO = struct.Struct('<QQ32s')
INFO = struct.Struct('<IIIIQQIIQ')
INFO_COMMAND = 0xfffe
QUIT_COMMAND = 0xffff


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size-len(data))
        if not chunk:
            raise RuntimeError('privileged N1X broker terminated unexpectedly')
        data.extend(chunk)
    return bytes(data)


def _descriptor(tensor):
    value = Descriptor()
    value.name = tensor.name.encode()
    value.size = tensor.bytes
    value.n, value.channels, value.h, value.w = tensor.shape
    for index, item in enumerate(tensor.format_codes): value.codes[index] = item
    for index, item in enumerate(tensor.strides): value.strides[index] = item
    return value


class Module:
    def __init__(self, device, index, model):
        if not (1 <= len(model.inputs) <= 64 and 1 <= len(model.outputs) <= 64):
            raise ValueError('broker supports 1..64 inputs and outputs')
        self.device, self.index = device, index
        self.inputs = [_descriptor(t) for t in model.inputs]
        self.outputs = [_descriptor(t) for t in model.outputs]
        self.input, self.output = self.inputs[0], self.outputs[0]
        self.total_input_bytes = sum(d.size for d in self.inputs)
        self.total_output_bytes = sum(d.size for d in self.outputs)
        self.failed = False

    def describe(self):
        def _fmt(desc):
            return {
                'name': desc.name.decode('utf-8', errors='replace'),
                'size': int(desc.size),
                'shape': list(desc.shape),
                'dataFormat': int(desc.codes[0]),
                'dataType': int(desc.codes[1]),
                'dataCategory': int(desc.codes[2]),
                'pixelFormat': int(desc.codes[3]),
                'pixelMapping': int(desc.codes[4]),
                'stride': [int(x) for x in desc.strides],
            }
        return {
            'inputs': [_fmt(d) for d in self.inputs],
            'outputs': [_fmt(d) for d in self.outputs],
            'total_input_bytes': self.total_input_bytes,
            'total_output_bytes': self.total_output_bytes,
        }

    def packed(self, data):
        if self.failed:
            raise RuntimeError('module stopped after a previous failure')
        try:
            return self.device._request(self.index, bytes(data), self.total_output_bytes)
        except Exception:
            self.failed = True
            raise

    def __call__(self, data):
        if len(self.inputs) == 1 and not isinstance(data, (dict, list, tuple)):
            packed_in = bytes(pack_tensor(data, self.input))
        elif isinstance(data, dict):
            chunks = []
            for d in self.inputs:
                key = d.name.decode('utf-8', errors='replace')
                if key not in data:
                    raise ValueError(f'missing input tensor {key}')
                chunks.append(bytes(pack_tensor(data[key], d)))
            packed_in = b''.join(chunks)
        else:
            if len(data) != len(self.inputs):
                raise ValueError('input tensor count mismatch')
            packed_in = b''.join(bytes(pack_tensor(arr, d)) for arr, d in zip(data, self.inputs))
        raw_out = self.packed(packed_in)
        if len(self.outputs) == 1:
            return unpack_tensor(raw_out, self.output)
        results = []
        cursor = 0
        for d in self.outputs:
            results.append(unpack_tensor(raw_out[cursor:cursor + d.size], d))
            cursor += d.size
        return results


class Device:
    """One authenticated broker process holding all requested models."""
    def __init__(self, models):
        if isinstance(models, dict):
            items = list(models.items())
        else:
            items = [(Path(path).stem, path) for path in models]
        if not items or len(items) > 64:
            raise ValueError('broker requires 1..64 models')
        try:
            status = BROKER.lstat()
        except FileNotFoundError:
            raise RuntimeError('secure N1X broker is not installed; see docs/setup.md')
        if (not stat.S_ISREG(status.st_mode) or status.st_uid or status.st_mode & 0o022 or
                not status.st_mode & 0o111):
            raise RuntimeError('N1X broker must be an executable, root-owned, non-writable regular file')
        self.definitions = []
        seen = set()
        for identifier, path in items:
            if not identifier or len(identifier) > 64 or any(not (c.islower() or c.isdigit() or c == '-') for c in identifier):
                raise ValueError('invalid broker model identifier')
            if identifier in seen:
                raise ValueError('duplicate broker model identifier')
            seen.add(identifier)
            path = Path(path).resolve(strict=True)
            raw = path.read_bytes()
            model = Model(raw)
            self.definitions.append((identifier, path, model, hashlib.sha256(raw).digest()))
        command = ([str(BROKER)] if os.geteuid() == 0 else ['pkexec', str(BROKER)]) + [row[0] for row in self.definitions]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None, close_fds=True)
        self.closed = False
        magic, version, count, size = HEADER.unpack(_exact(self.process.stdout, HEADER.size))
        if magic != b'N1XB' or version != PROTOCOL or count != len(items) or size != count*MODEL_INFO.size:
            self._abort_startup('invalid broker greeting')
        records = _exact(self.process.stdout, size)
        self.modules = []
        for index, (_, _, model, digest) in enumerate(self.definitions):
            input_bytes, output_bytes, installed_digest = MODEL_INFO.unpack_from(records, index*MODEL_INFO.size)
            expected_in = sum(t.bytes for t in model.inputs)
            expected_out = sum(t.bytes for t in model.outputs)
            if not model.inputs or not model.outputs or input_bytes != expected_in or output_bytes != expected_out:
                self._abort_startup('installed model tensor metadata differs')
            if installed_digest != digest:
                self._abort_startup('installed model hash differs from the reviewed local artifact')
            self.modules.append(Module(self, index, model))

    def _abort_startup(self, message):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(message+'; broker did not stop after its input closed') from error
        raise RuntimeError(message)

    def _request(self, code, payload=b'', expected=None):
        if self.closed or self.process.poll() is not None:
            raise RuntimeError('N1X broker is not running')
        self.process.stdin.write(HEADER.pack(b'N1XQ', PROTOCOL, code, len(payload)))
        if payload: self.process.stdin.write(payload)
        self.process.stdin.flush()
        magic, version, response_code, size = HEADER.unpack(_exact(self.process.stdout, HEADER.size))
        if magic != b'N1XR' or version != PROTOCOL or response_code != code or (expected is not None and size != expected):
            raise RuntimeError('invalid N1X broker response')
        return _exact(self.process.stdout, size)

    def load(self, path_or_name):
        requested = str(path_or_name)
        for index, (identifier, path, _, _) in enumerate(self.definitions):
            if requested == identifier or Path(requested).resolve() == path:
                return self.modules[index]
        raise ValueError('model was not selected when the secure broker started')

    def info(self):
        fields = INFO.unpack(self._request(INFO_COMMAND, expected=INFO.size))
        return dict(abi_version=fields[0], capabilities=fields[1], max_image_bytes=fields[2],
                    max_addresses=fields[3], retained_bytes=fields[4], retain_limit=fields[5],
                    retained_jobs=fields[6], max_jobs=fields[7])

    def close(self):
        if self.closed: return
        self.closed = True
        if self.process.poll() is None:
            try:
                self.process.stdin.write(HEADER.pack(b'N1XQ', PROTOCOL, QUIT_COMMAND, 0))
                self.process.stdin.flush()
            finally:
                self.process.stdin.close()
            status = self.process.wait(timeout=10)
            if status:
                raise RuntimeError(f'N1X broker exited with status {status}')

    def __enter__(self): return self
    def __exit__(self, exc_type, *_):
        try: self.close()
        except Exception:
            if exc_type is None: raise
