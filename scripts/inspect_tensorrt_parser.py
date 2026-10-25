#!/usr/bin/env python3
"""Audit the exact Windows ARM ONNX parser acquired for the compiler study."""
import hashlib
import json
from pathlib import Path
import pefile

ROOT=Path(__file__).resolve().parents[1]
SIGNED='ad44a8a9be9ab824540b6363b8cb6e63f0d879b97c27b71ef4fde50c59068207'
PRE_SIGN='eb2896c3cfce1c91a2e4619b0296fc13b3a3447d9f7930c73a1aeca2a2990306'


def main():
    path=ROOT/'sources/tensorrt-11.4/nvonnxparser_11.dll'
    data=path.read_bytes()
    assert hashlib.sha256(data).hexdigest()==SIGNED
    pe=pefile.PE(data=data)
    assert pe.FILE_HEADER.Machine==0xaa64
    security=pe.OPTIONAL_HEADER.DATA_DIRECTORY[4]
    assert (security.VirtualAddress,security.Size)==(2260480,10352)
    assert security.VirtualAddress+security.Size==len(data)
    # A reconstruction solely for comparison with the wheel RECORD's pre-sign
    # digest. Never overwrite the acquired signed file or claim trust validation.
    original=bytearray(data[:security.VirtualAddress])
    checksum=pe.OPTIONAL_HEADER.get_field_absolute_offset('CheckSum')
    original[checksum:checksum+4]=bytes(4)
    original[security.get_file_offset():security.get_file_offset()+8]=bytes(8)
    assert hashlib.sha256(original).hexdigest()==PRE_SIGN
    strings=[b'checkDLASupport',b'kREPORT_CAPABILITY_DLA',b'DLA version >= 3.16']
    assert all(value in data for value in strings)
    imports={d.dll.decode():[(i.name or b'').decode() for i in d.imports]
             for d in pe.DIRECTORY_ENTRY_IMPORT}
    exports={e.name.decode():hex(e.address) for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
    print(json.dumps(dict(passed=True,hardware_access=False,machine='ARM64',bytes=len(data),
                         acquired_sha256=SIGNED,reconstructed_pre_sign_sha256=PRE_SIGN,
                         imports=imports,exports=exports,dla_strings=[s.decode() for s in strings],
                         limitations=['File hash consistency only; no Authenticode certificate-chain trust validation',
                                      'Strings identify code paths, not proof that a model compiles or executes',
                                      'TensorRT wheel download and separate DLA compiler acquisition are independent pending work']),indent=2))


if __name__=='__main__':main()
