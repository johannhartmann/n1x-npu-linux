#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check pinned local inputs; optionally fetch public assets or extract firmware.

Writes only ignored sources/ and results/. Never installs packages, accesses
devices, loads modules, changes boot configuration or executes a vendor installer.
"""
import argparse
import hashlib
import io
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    ('sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive.tar.xz',
     'https://developer.download.nvidia.com/compute/cuda/redist/libcudla/linux-sbsa/libcudla-linux-sbsa-13.4.92-archive.tar.xz',
     '2842ab55ebcf92d3dafb8836f2952d84637ee7bec7072e49293aacdb44d0c7e6'),
    ('sources/tensorrt-11.4/libnvinfer-dla-compiler11_11.4.0.106-1+cuda13.4_arm64.deb',
     'https://developer.download.nvidia.com/compute/cuda/repos/debian12/sbsa/libnvinfer-dla-compiler11_11.4.0.106-1+cuda13.4_arm64.deb',
     '9954f3c1d88836767a0657e39e2050c32499b950860524e8e6dc8d0ecc48a2cc'),
    ('sources/demo-research/super-resolution-10.onnx',
     'https://media.githubusercontent.com/media/onnx/models/main/validated/vision/super_resolution/sub_pixel_cnn_2016/model/super-resolution-10.onnx',
     '85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e'),
    ('sources/demo-research/emotion-ferplus-8.onnx',
     'https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx',
     'a2a2ba6a335a3b29c21acb6272f962bd3d47f84952aaffa03b60986e04efa61c'),
    ('sources/demo-research/mnist-12.onnx',
     'https://media.githubusercontent.com/media/onnx/models/main/validated/vision/classification/mnist/model/mnist-12.onnx',
     '5c688690f8bacf667d4c2074af5ad0646ca328d7ab03eccf944a65b320171bdd'),
    ('sources/demo-research/astronaut.png',
     'https://raw.githubusercontent.com/scikit-image/scikit-image/v0.19.3/skimage/data/astronaut.png',
     '88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5'),
)
FRONTEND = ('sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive/lib/libcudla.so.1.0.0',
            '89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9')
NATIVE = ('sources/tensorrt-11.4/native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so',
          '28668e74ced3c04ca34f30d41b7ea1d39cabe791418fbad31fcfb009d5558ad9')
WINDOWS = ('sources/tensorrt-11.4/nvdla_compiler.dll',
           '0ef7c9366205a7151490f07b0df94a5e6908c94da32e83fabaaef5d06254147d')
MANUAL = (
    ('sources/nvidia/Display.Driver/nvlddmkm.sys',
     '73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987'),
    ('sources/nvidia/Display.Driver/nvdla_runtime.dll',
     'c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b'),
)
FIRMWARE = (
    ('sources/dla-p16-sroot-v0.bin', 0x1405800, 67508,
     '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4'),
    ('sources/dla-p16-sroot-v1.bin', 0x13f5040, 67508,
     '6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4'),
)


def _pe_slice(pe_bytes, rva, size):
    e_lfanew = struct.unpack_from('<I', pe_bytes, 0x3c)[0]
    if pe_bytes[e_lfanew:e_lfanew + 4] != b'PE\x00\x00':
        raise ValueError('invalid PE signature')
    machine, num_sections = struct.unpack_from('<HH', pe_bytes, e_lfanew + 4)
    opt_size = struct.unpack_from('<H', pe_bytes, e_lfanew + 20)[0]
    if machine != 0xaa64:
        raise ValueError('expected ARM64 PE image')
    sec_base = e_lfanew + 24 + opt_size
    for i in range(num_sections):
        off = sec_base + i * 40
        vsize, va, raw_size, raw_ptr = struct.unpack_from('<IIII', pe_bytes, off + 8)
        span = max(vsize, raw_size)
        if va <= rva and rva + size <= va + span:
            file_off = raw_ptr + (rva - va)
            return pe_bytes[file_off:file_off + size]
    raise ValueError(f'RVA {hex(rva)} not found in PE sections')


def verified(path, expected):
    if not path.exists():
        return False
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != expected:
        raise ValueError(f'hash mismatch: {path}; existing file was not overwritten')
    return True


def download(relative, url, expected):
    path = ROOT/relative
    if verified(path, expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                shutil.copyfileobj(response, stream)
            stream.close()
            verified(temporary, expected)
            path.hardlink_to(temporary)
        finally:
            temporary.unlink(missing_ok=True)


def _fetch_windows_compiler():
    url = 'https://developer.download.nvidia.com/compute/machine-learning/tensorrt/11.4.0/zip/TensorRT-Enterprise-11.4.0.106-Windows-arm64-cuda-13.4-Release-external.zip'
    size = 332016940

    def get_range(a, b):
        req = urllib.request.Request(url, headers={'Range': f'bytes={a}-{b}'})
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status != 206:
                raise ValueError(f'expected HTTP 206 Partial Content, got {resp.status}')
            data = resp.read()
            if len(data) != b - a + 1:
                raise ValueError('short HTTP range response')
            return data

    tail = get_range(size - 65536, size - 1)
    idx = tail.rfind(b'PK\x05\x06')
    if idx < 0:
        raise ValueError('missing ZIP end-of-central-directory record')
    _, disk, cdisk, n1, n2, cs, co, _ = struct.unpack_from('<4s4H2IH', tail, idx)
    if disk != 0 or cdisk != 0 or n1 != n2:
        raise ValueError('unexpected multi-disk ZIP archive')
    cd = get_range(co, co + cs - 1)
    pos = 0
    while pos < len(cd):
        hdr = struct.unpack_from('<4s6H3I5H2I', cd, pos)
        if hdr[0] != b'PK\x01\x02':
            raise ValueError('corrupt ZIP central directory entry')
        nl, xl, cl = hdr[10:13]
        name = cd[pos + 46:pos + 46 + nl].decode('utf-8')
        pos += 46 + nl + xl + cl
        if name.lower().endswith('nvdla_compiler.dll'):
            method, crc, comp_size, uncomp_size, offset = hdr[4], hdr[7], hdr[8], hdr[9], hdr[-1]
            lhdr = get_range(offset, offset + 29)
            lfields = struct.unpack('<4s5H3I2H', lhdr)
            start = offset + 30 + lfields[-2] + lfields[-1]
            compressed = get_range(start, start + comp_size - 1)
            raw = zlib.decompress(compressed, -15) if method == 8 else compressed
            if len(raw) != uncomp_size or (zlib.crc32(raw) & 0xffffffff) != crc:
                raise ValueError('Windows compiler ZIP CRC/size mismatch')
            if hashlib.sha256(raw).hexdigest() != WINDOWS[1]:
                raise ValueError('Windows compiler SHA-256 mismatch')
            target = ROOT / WINDOWS[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(raw)
            return
    raise ValueError('nvdla_compiler.dll not found in TensorRT ZIP archive')


def unpack_public():
    archive = ROOT/ASSETS[0][0]
    destination = (ROOT/FRONTEND[0]).parents[1]
    if not verified(ROOT/FRONTEND[0], FRONTEND[1]):
        if destination.exists():
            raise ValueError(f'incomplete extraction exists: {destination}; inspect it before continuing')
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter='data')
            staged = Path(tmp)/destination.name
            if not verified(staged/'lib/libcudla.so.1.0.0', FRONTEND[1]):
                raise ValueError('missing cuDLA library')
            if not (staged/'include/cudla.h').is_file():
                raise ValueError('cuDLA headers missing from archive')
            staged.rename(destination)
    if not verified(ROOT/NATIVE[0], NATIVE[1]):
        data = subprocess.check_output(['ar', 'p', str(ROOT/ASSETS[1][0]), 'data.tar.xz'])
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:xz') as tar:
            member = next(m for m in tar.getmembers()
                          if m.name.removeprefix('./') == 'usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so')
            if not member.isfile():
                raise ValueError('native compiler must be an ordinary file')
            payload = tar.extractfile(member).read()
        if hashlib.sha256(payload).hexdigest() != NATIVE[1]:
            raise ValueError('native compiler hash mismatch')
        path = ROOT/NATIVE[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(payload)
    if not verified(ROOT/WINDOWS[0], WINDOWS[1]):
        _fetch_windows_compiler()
        if not verified(ROOT/WINDOWS[0], WINDOWS[1]):
            raise ValueError('Windows compiler missing after extraction')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='download hash-pinned public assets and unpack compilers locally')
    parser.add_argument('--extract-firmware', action='store_true', help='extract pinned SROOT firmware from the locally supplied Windows KMD')
    args = parser.parse_args()
    try:
        if args.fetch:
            for item in ASSETS:
                download(*item)
            unpack_public()
        if args.extract_firmware:
            kmd = ROOT/MANUAL[0][0]
            if not verified(kmd, MANUAL[0][1]):
                raise ValueError('supply the pinned Windows KMD first; see docs/setup.md')
            pe_bytes = kmd.read_bytes()
            for relative, rva, size, digest in FIRMWARE:
                target = ROOT/relative
                if not verified(target, digest):
                    blob = _pe_slice(pe_bytes, rva, size)
                    if hashlib.sha256(blob).hexdigest() != digest:
                        raise ValueError(f'extracted firmware hash mismatch: {target}')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as stream:
                        stream.write(blob)
        required = [FRONTEND, NATIVE, WINDOWS, *MANUAL,
                    *((p, sha) for p, _, _, sha in FIRMWARE),
                    *((p, sha) for p, _, sha in ASSETS[2:])]
        missing = []
        for relative, digest in required:
            ok = verified(ROOT/relative, digest)
            print(('OK      ' if ok else 'MISSING ')+relative)
            if not ok:
                missing.append(relative)
        if missing:
            print('See docs/setup.md for public downloads and manually supplied vendor inputs.', file=sys.stderr)
            return 1
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
