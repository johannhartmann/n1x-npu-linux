#!/usr/bin/env python3
"""Package the pinned compiler output and independent input for one NPU test.

No model compilation, device access, address publication or vendor-byte
redistribution. The kernel supplies real IOVAs and checks the whole template.
"""
import hashlib
import json
from pathlib import Path

from diagnostic_fixture import build_relu
from verify_relu_loadable import LOADABLE_HASH

ROOT = Path(__file__).resolve().parents[1]
INPUT_SHA256 = '73cc501f702fa3fdd1a468fefc316d1b838941fdc1397d4a7ff9b30ee4a81f45'
EXPECTED_SHA256 = '6e043c4c2ae0ddfcaca4341ab36f15fdebf066157af5da6483bc56cd6786404f'


def main():
    model = (ROOT/'results/native-compile-t254/relu.nvda').read_bytes()
    assert hashlib.sha256(model).hexdigest() == LOADABLE_HASH
    payload, addresses, layout = build_relu(model, 0)
    assert len(payload) == 2834432 and payload[:1024] == bytes(1024)
    assert addresses == [16384, 8192, 12288, 20480, 53248, 57344, 2797568, 2830336]
    input_data = (ROOT/'results/relu-probe/input-chw16-fp16.bin').read_bytes()
    expected = (ROOT/'results/relu-probe/expected-chw16-fp16.bin').read_bytes()
    assert len(input_data) == len(expected) == 3136
    assert hashlib.sha256(input_data).hexdigest() == INPUT_SHA256
    assert hashlib.sha256(expected).hexdigest() == EXPECTED_SHA256
    payload[8192:8192+3136] = input_data
    payload[12288:12288+3136] = b'\xa5'*3136
    path = ROOT/'results/p16-relu-template.bin'
    if path.exists():
        assert path.read_bytes() == payload, 'existing template differs; not overwritten'
    else:
        with path.open('xb') as out:
            out.write(payload)
    print(json.dumps(dict(template=str(path), bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(), loadable_sha256=LOADABLE_HASH,
        input_sha256=INPUT_SHA256, expected_sha256=EXPECTED_SHA256,
        address_offsets=addresses, layout=layout, hardware_access=False,
        numerical_execution_verified=False), indent=2))


if __name__ == '__main__':
    main()
