#!/usr/bin/env python3
"""Check broker confinement/preflight invariants without root or device access."""
import ctypes as c
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
from loadable import Model


def main():
    source = (ROOT/'runtime/cudla_broker.cpp').read_text()
    start = source.index('void drop_privileges(')
    drop = source[start:source.index('\n}\n}', start)]
    assert drop.index('setgid(identity.gid)') < drop.index('bind_to_parent(parent)')
    assert drop.index('setuid(identity.uid)') < drop.index('bind_to_parent(parent)')
    assert 'getpwnam_r("n1x-dla"' in source
    assert 'PR_GET_PDEATHSIG' in source and 'protocol_timeout_ms=60000' in source
    assert 'duplicate model identifier' in source and 'max_total_model_bytes' in source
    assert 'insufficient retained-DMA budget for complete broker session' in source

    paths = [
        ROOT/'results/native-compile-t254/relu.nvda',
        ROOT/'results/native-compile-t254-superres/superres.nvda',
        ROOT/'results/native-compile-t254-resnet18-b7/resnet18-b7.nvda',
    ]
    library = c.CDLL(str(ROOT/'runtime/cudla/libnvcudla.so'))
    query = library.n1x_cudla_materialized_image_size
    query.argtypes = (c.c_void_p, c.c_size_t, c.POINTER(c.c_uint64))
    query.restype = c.c_int
    for path in paths:
        raw = path.read_bytes(); expected = len(Model(raw).image)
        data = c.create_string_buffer(raw); result = c.c_uint64()
        assert query(data, len(raw), c.byref(result)) == 0 and result.value == expected
    invalid = c.create_string_buffer(b'not a loadable'); result = c.c_uint64()
    assert query(invalid, len(invalid.raw), c.byref(result)) < 0
    assert query(None, 0, c.byref(result)) < 0
    print('Broker confinement order, deadlines, aggregate guards and exact retention preflight passed; no root/device access')


if __name__ == '__main__': main()
