#!/usr/bin/env python3
"""Verify all four signed upload variants and identify the raw HAL-1 fallback."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile

from extract_profile1_firmware import extract_boot_variant, PROFILE1_HASH
from inspect_sroot_envelope import ROOT, Tag, decode, loads, encode, verify


def inspect(image):
    assert struct.unpack_from('<I', image)[0] == 0xc0302
    packed, body_start = decode(image, 4)
    assert body_start == 404 and len(packed) == 397
    outer = loads(packed)
    assert len(outer) == 2 and outer[1] == bytes(6)
    assert len(outer[0]) == len(outer[0][0]) == 1
    tagged = outer[0][0][0]
    assert isinstance(tagged, Tag) and tagged.number == 98
    protected, unprotected, payload, signatures = tagged.value
    assert protected == b'' and unprotected == {} and len(signatures) == 1
    signer_protected, signer_unprotected, signature = signatures[0]
    assert signer_unprotected == {} and len(signature) == 96
    headers, claims = loads(signer_protected), loads(payload)
    assert headers[1] == -35 and headers[4] == b'08'
    key = headers[-1277951]
    assert (key[1], key[2], key[3], key[-1], key[-1310718]) == (2, b'08', -35, 2, 12)
    assert len(key[-2]) == len(key[-3]) == 48
    public_der = bytes.fromhex('3076301006072a8648ce3d020106052b8104002203620004')+key[-2]+key[-3]
    message = encode(['Signature', protected, signer_protected, b'', payload])
    assert verify(public_der, signature, message)
    assert claims[-1310719] == 80
    digests = claims[-1294334]
    ranges = [(404, 484), (484, len(image))]
    assert len(digests) == 2
    for (start, end), (algorithm, digest) in zip(ranges, digests):
        assert algorithm == -43 and len(digest) == 48
        assert hashlib.sha384(image[start:end]).digest() == digest
        changed = bytearray(image[start:end])
        changed[-1] ^= 1
        assert hashlib.sha384(changed).digest() != digest
    assert struct.unpack_from('<I', image, 488)[0] == 208
    assert image[500:504] == b'DLA\0'
    return {'public_der': public_der, 'signature': signature, 'message': message,
            'claims': claims[-1277952], 'body': image[404:484],
            'wrapper': image[484:692], 'payload': image[692:],
            'sha256': hashlib.sha256(image).hexdigest()}


def main():
    raw = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert len(raw) == 66816 and hashlib.sha256(raw).hexdigest() == PROFILE1_HASH
    entries, rows = {}, []
    # remapper 1 selects group 0; modes 0 and 2 share group 1, as verified by
    # emulate_boot_variants.py. Keep group selection separate from variant.
    with tempfile.TemporaryDirectory(prefix='n1x-signed-variants-') as tmp:
        for remapper in (1, 0):
            for variant in (0, 1):
                output = Path(tmp)/f'{remapper}-{variant}.bin'
                meta = extract_boot_variant(ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys',
                                            output, remapper, variant)
                image = output.read_bytes()
                entry = inspect(image)
                entries[remapper, variant] = entry
                rows.append({'remapper': remapper, 'variant': variant,
                    'image_rva': meta['firmware_rva'], 'image_bytes': len(image),
                    'image_sha256': entry['sha256'],
                    'public_key_spki_sha256': hashlib.sha256(entry['public_der']).hexdigest(),
                    'embedded_key_id': '08', 'signature_and_two_body_digests_verified': True,
                    'payload_offset': 692, 'payload_bytes': len(entry['payload']),
                    'payload_sha256': hashlib.sha256(entry['payload']).hexdigest(),
                    'equals_raw_hal1_image': entry['payload'] == raw,
                    'wrapper_words_48_52': list(struct.unpack_from('<II', entry['wrapper'], 48)),
                    'vendor_claims': entry['claims']})
    # The vendor-signed fallback wraps the complete, unmodified image used in
    # our original RISC-V scheduler tests, not merely a few matching samples.
    assert entries[1, 1]['payload'] == raw
    assert all(entry['payload'] != raw for key, entry in entries.items() if key != (1, 1))
    for remapper in (1, 0):
        primary, fallback = entries[remapper, 0], entries[remapper, 1]
        assert primary['body'] == fallback['body']
        assert primary['public_der'] != fallback['public_der']
        assert primary['payload'] != fallback['payload']
        assert struct.unpack_from('<II', primary['wrapper'], 48) == (1, 3)
        assert struct.unpack_from('<II', fallback['wrapper'], 48) == (0, 0)
        assert not verify(primary['public_der'], fallback['signature'], fallback['message'])
        assert not verify(fallback['public_der'], primary['signature'], primary['message'])
    for variant in (0, 1):
        assert entries[1, variant]['public_der'] == entries[0, variant]['public_der']
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
        'cross_key_rejection_controls': 4, 'body_mutation_controls': 8,
        'exact_raw_hal1_location': {'remapper': 1, 'variant': 1, 'offset': 692,
                                   'bytes': len(raw), 'sha256': PROFILE1_HASH},
        'limitations': ['The hardware trust root for either embedded key is unknown',
            'Key labels, wrapper flags and vendor claims have no inferred production/debug meanings',
            'Variant 0 transformation is not identified by this comparison',
            'Equality proves the fallback contains the raw image, not that hardware accepts or runs it',
            'No image modification, signature replacement, hardware access or upload retry']}, indent=2))


if __name__ == '__main__':
    main()
