#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check corrupt downloads and existing-file preservation without networking."""
import hashlib
import io
from pathlib import Path
import tempfile
from unittest.mock import patch

import prepare_inputs as inputs


def main():
    payload = b'locally modeled download'
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix='n1x-inputs-') as directory:
        root = Path(directory)
        path = root/'sources/input.bin'
        with patch.object(inputs, 'ROOT', root), \
             patch.object(inputs.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as network:
            assert not inputs.verified(path, digest)
            inputs.download('sources/input.bin', 'https://example.invalid/input', digest)
            assert path.read_bytes() == payload and inputs.verified(path, digest)
            network.assert_called_once()
        with patch.object(inputs, 'ROOT', root), patch.object(inputs.urllib.request, 'urlopen') as network:
            inputs.download('sources/input.bin', 'https://example.invalid/input', digest)
            network.assert_not_called()
            path.write_bytes(b'preserve existing mismatched input')
            try:
                inputs.download('sources/input.bin', 'https://example.invalid/input', digest)
                raise AssertionError('mismatched input accepted')
            except ValueError:
                assert path.read_bytes() == b'preserve existing mismatched input'
            network.assert_not_called()
        with patch.object(inputs, 'ROOT', root), \
             patch.object(inputs.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupted')):
            try:
                inputs.download('sources/bad.bin', 'https://example.invalid/input', digest)
                raise AssertionError('corrupt download accepted')
            except ValueError:
                assert not (root/'sources/bad.bin').exists()
                assert sorted(p.name for p in (root/'sources').iterdir()) == ['input.bin']
    print('Input acquisition: verified download/cache, mismatch preservation and corrupt-file cleanup passed; no network')


if __name__ == '__main__':
    main()
