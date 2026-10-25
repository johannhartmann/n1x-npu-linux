#!/usr/bin/env python3
"""Inspect/verify the pinned SROOT COSE envelope offline; no trust-root claim."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE_HASH = '0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4'
RAW_HASH = '2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997'


@dataclass(frozen=True)
class Tag:
    number: int
    value: object


def decode(data, at=0, depth=0):
    """Bounded definite-length CBOR subset needed by this exact envelope."""
    if depth > 16 or not 0 <= at < len(data):
        raise ValueError('CBOR nesting/extent')
    byte = data[at]
    at += 1
    major, additional = byte >> 5, byte & 31
    if additional < 24:
        number = additional
    elif additional in (24, 25, 26, 27):
        width = 1 << (additional-24)
        if width > len(data)-at:
            raise ValueError('truncated CBOR argument')
        number = int.from_bytes(data[at:at+width], 'big')
        at += width
    else:
        raise ValueError('unsupported CBOR length')
    if major in (0, 1):
        return (number if major == 0 else -1-number), at
    if major in (2, 3):
        if number > len(data)-at:
            raise ValueError('truncated CBOR string')
        value = data[at:at+number]
        return (value if major == 2 else value.decode('utf-8')), at+number
    if major in (4, 5):
        if number > 128:
            raise ValueError('CBOR collection limit')
        values = []
        for _ in range(number*(2 if major == 5 else 1)):
            value, at = decode(data, at, depth+1)
            values.append(value)
        if major == 4:
            return values, at
        output = {}
        for key, value in zip(values[::2], values[1::2]):
            if not isinstance(key, (int, str, bytes)) or key in output:
                raise ValueError('unsupported/duplicate CBOR map key')
            output[key] = value
        return output, at
    if major == 6:
        value, at = decode(data, at, depth+1)
        return Tag(number, value), at
    raise ValueError('unsupported CBOR type')


def loads(data):
    value, end = decode(data)
    if end != len(data):
        raise ValueError('trailing CBOR data')
    return value


def head(major, number):
    if number < 24:
        return bytes([major*32+number])
    for additional, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if number < 1 << (width*8):
            return bytes([major*32+additional])+number.to_bytes(width, 'big')
    raise ValueError('CBOR integer too large')


def encode(value):
    if isinstance(value, bytes):
        return head(2, len(value))+value
    if isinstance(value, str):
        data = value.encode()
        return head(3, len(data))+data
    if isinstance(value, list):
        return head(4, len(value))+b''.join(map(encode, value))
    raise ValueError('unsupported signature encoding input')


def der(tag, data):
    if len(data) >= 128:
        raise ValueError('unexpected DER length')
    return bytes([tag, len(data)])+data


def integer(data):
    data = data.lstrip(b'\0') or b'\0'
    return der(2, (b'\0' if data[0] & 128 else b'')+data)


def verify(public_der, signature, message):
    with tempfile.TemporaryDirectory(prefix='n1x-cose-') as tmp:
        path = Path(tmp)
        (path/'public.der').write_bytes(public_der)
        (path/'signature.der').write_bytes(der(0x30, integer(signature[:48])+integer(signature[48:])))
        (path/'message.cbor').write_bytes(message)
        subprocess.run(['openssl', 'pkey', '-pubin', '-inform', 'DER', '-in', str(path/'public.der'),
                        '-out', str(path/'public.pem')], check=True, capture_output=True)
        result = subprocess.run(['openssl', 'dgst', '-sha384', '-verify', str(path/'public.pem'),
                                 '-signature', str(path/'signature.der'), str(path/'message.cbor')],
                                capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr)
        return result.returncode == 0


def main():
    image = (ROOT/'sources/dla-p16-sroot-v0.bin').read_bytes()
    raw = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(image).hexdigest() == IMAGE_HASH
    assert hashlib.sha256(raw).hexdigest() == RAW_HASH
    assert len(image) == 67508 and struct.unpack_from('<I', image)[0] == 0xc0302
    packed, body_offset = decode(image, 4)
    assert isinstance(packed, bytes) and len(packed) == 397 and body_offset == 0x194
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
    assert (key[1], key[2], key[3], key[-1]) == (2, b'08', -35, 2)
    assert len(key[-2]) == len(key[-3]) == 48
    public_der = bytes.fromhex('3076301006072a8648ce3d020106052b8104002203620004')+key[-2]+key[-3]
    message = encode(['Signature', protected, signer_protected, b'', payload])
    assert verify(public_der, signature, message)
    bad_signature = bytes([signature[0] ^ 1])+signature[1:]
    assert not verify(public_der, bad_signature, message)
    bad_payload = bytes([payload[0] ^ 1])+payload[1:]
    assert not verify(public_der, signature, encode(['Signature', protected, signer_protected, b'', bad_payload]))
    assert not verify(public_der, signature, encode(['Signature', protected, signer_protected, outer[1], payload]))
    lengths = claims[-1310719]
    assert lengths == 80
    digests = claims[-1294334]
    assert len(digests) == 2 and all(row[0] == -43 and len(row[1]) == 48 for row in digests)
    ranges = [(body_offset, body_offset+lengths), (body_offset+lengths, len(image))]
    for (start, end), (_, digest) in zip(ranges, digests):
        assert hashlib.sha384(image[start:end]).digest() == digest
        changed = bytearray(image[start:end])
        changed[len(changed)//2] ^= 1
        assert hashlib.sha384(changed).digest() != digest
    assert image.find(raw[512:576]) == image.find(raw[37376:37440]) == -1
    raw_candidates = {'whole': raw, 'payload': raw[512:], 'code': raw[512:37376], 'data': raw[37376:]}
    assert all(hashlib.sha384(data).digest() not in [row[1] for row in digests] for data in raw_candidates.values())
    parser_controls = 0
    for invalid in (b'', b'\x59', b'\x59\xff\xff', b'\x9f', b'\x98\x81', b'\xa2\x01\x00\x01\x01', packed[:-1], packed+b'\x00'):
        try:
            loads(invalid)
        except ValueError:
            parser_controls += 1
        else:
            raise AssertionError('malformed CBOR accepted')
    print(json.dumps({'passed': True, 'hardware_access': False, 'image_sha256': IMAGE_HASH,
                      'cbor_prefix_bytes': body_offset-4, 'cose_tag': 98, 'algorithm': 'ES384',
                      'embedded_key_curve': 'P-384', 'embedded_key_id_hex': headers[4].hex(),
                      'public_key_spki_sha256': hashlib.sha256(public_der).hexdigest(),
                      'signature_verifies_with_embedded_key': True, 'external_aad_bytes': 0,
                      'trust_root_verified': False, 'hardware_authentication_verified': False,
                      'signature_negative_controls': 3, 'body_digest_negative_controls': 2,
                      'malformed_cbor_controls': parser_controls,
                      'signed_body_ranges': [{'start': start, 'end': end, 'bytes': end-start,
                                              'sha384': digest[1].hex()} for (start, end), digest in zip(ranges, digests)],
                      'vendor_claims': {str(k): v for k, v in claims[-1277952].items()},
                      'raw_firmware_identity_proven': False,
                      'limitations': ['Embedded-key signature verification establishes mathematical consistency, not vendor trust',
                                      'Vendor claim meanings, root-of-trust policy and transport acceptance remain unverified',
                                      'Signed-body equality to raw firmware is not established; no decryption/compression interpretation inferred',
                                      'No device writes, authentication bypass, physical firmware boot, DMA or inference']}, indent=2))


if __name__ == '__main__':
    main()
