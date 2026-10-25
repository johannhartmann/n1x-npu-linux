#!/usr/bin/env python3
"""Verify/extract the pinned DGX Spark OSROOT research input; never flash it."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

import capstone
from inspect_sroot_envelope import ROOT, Tag, decode, loads, encode
from inspect_sroot_envelope import verify as verify_ec

CAPSULE_HASH = '00393be197569851c9a72bd214cf22f45a04b7e1fc759ab4ccc68c98dd650d78'
PREFIX = 0x8d5c5f
PAYLOAD_HASHES = (
    '411558877583f837f6c0ba2b2e1df42122296067d9ae5cc83906bd915db0dce2',
    '08adaedac470ec367e74a644663ab68b92023fc47178aaec896f2a1ae468eb47',
    '1086a49e17844e2aad6990a70f1c25a979ecf08f09991e8ce7f203da8791b7d8',
)


def der(tag, data):
    size = len(data)
    encoded = size.to_bytes((size.bit_length()+7)//8, 'big')
    length = bytes([size]) if size < 128 else bytes([0x80 | len(encoded)])+encoded
    return bytes([tag])+length+data


def integer(data):
    data = data.lstrip(b'\0') or b'\0'
    return der(2, (b'\0' if data[0] & 128 else b'')+data)


def verify(public, signature, message):
    with tempfile.TemporaryDirectory(prefix='n1x-osroot-rsa-') as tmp:
        path = Path(tmp)
        (path/'public.der').write_bytes(public)
        (path/'signature').write_bytes(signature)
        (path/'message').write_bytes(message)
        subprocess.run(['openssl', 'rsa', '-RSAPublicKey_in', '-inform', 'DER',
                        '-in', str(path/'public.der'), '-pubout', '-out', str(path/'public.pem')],
                       check=True, capture_output=True)
        result = subprocess.run(['openssl', 'dgst', '-sha384', '-verify', str(path/'public.pem'),
            '-signature', str(path/'signature'), '-sigopt', 'rsa_padding_mode:pss',
            '-sigopt', 'rsa_pss_saltlen:48', '-sigopt', 'rsa_mgf1_md:sha384', str(path/'message')],
            capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr)
        return result.returncode == 0


def inspect_sroot(capsule):
    """Verify the separate SROOT FW envelope; do not extract opaque payloads."""
    packed, body = decode(capsule, 0x23c5f)
    assert len(packed) == 1037 and body == 0x2406f
    outer = loads(packed)
    assert len(outer) == 3 and outer[-1] == bytes(8)
    signers, all_digests = [], []
    for entry, key_id in zip(outer[:-1], (b'09', b'00')):
        assert len(entry) == len(entry[0]) == 1
        tagged = entry[0][0]
        assert isinstance(tagged, Tag) and tagged.number == 98
        protected, unprotected, payload, signatures = tagged.value
        assert protected == b'' and unprotected == {} and len(signatures) == 1
        signer, unsigned, signature = signatures[0]
        headers, claims = loads(signer), loads(payload)
        key = headers[-1277951]
        assert unsigned == {} and len(signature) == 96
        assert (headers[1], headers[4], key[1], key[2], key[3], key[-1]) == (
            -35, key_id, 2, key_id, -35, 2)
        assert len(key[-2]) == len(key[-3]) == 48
        public = bytes.fromhex('3076301006072a8648ce3d020106052b8104002203620004')+key[-2]+key[-3]
        message = encode(['Signature', protected, signer, b'', payload])
        assert verify_ec(public, signature, message)
        assert not verify_ec(public, bytes([signature[0] ^ 1])+signature[1:], message)
        assert not verify_ec(public, signature, message[:-1]+bytes([message[-1] ^ 1]))
        assert claims[-1310719] == 144
        all_digests.append(claims[-1294334])
        signers.append({'key_id': key_id.decode(), 'algorithm': 'ES384',
                        'public_key_spki_sha256': hashlib.sha256(public).hexdigest()})
    assert all_digests[0] == all_digests[1] and len(all_digests[0]) == 4
    assert struct.unpack_from('<4I', capsule, body) == (1, 4, 0xefbeadde, 0xefbeadde)
    expected = ((0, 0, 0, 144, ''), (0x1000, 0x90, 208, 0x3330, 'SROOT_FW_BPT-0.1.0'),
                (0x2100, 0x3490, 208, 0x2e5a0, 'SROOT_FW-0.1.0'),
                (0x2200, 0x31b00, 208, 0x10790, 'SROOT_FW-0.1.0'))
    components = []
    for index, (kind, offset, header, size, name) in enumerate(expected):
        words = struct.unpack_from('<8I', capsule, body+16+index*32)
        assert words[:5] == (0x101, kind, offset, header, size)
        assert words[-1] == 0xefbeadde
        start, end = body+offset, body+offset+header+size
        assert 0 <= start < end <= len(capsule)
        chunk = capsule[start:end]
        algorithm, digest = all_digests[0][index]
        assert algorithm == -43 and hashlib.sha384(chunk).digest() == digest
        assert hashlib.sha384(chunk[:-1]+bytes([chunk[-1] ^ 1])).digest() != digest
        if header:
            assert struct.unpack_from('<4I', chunk) == (1, 208, 0, 0xefbeadde)
            assert chunk[16:48].split(b'\0')[0].decode() == name
            assert struct.unpack_from('<II', chunk, 48) == (1, 3)
        components.append({'kind': hex(kind), 'name': name, 'signed_start': hex(start),
                           'signed_end': hex(end), 'payload_bytes': size,
                           'payload_sha256': hashlib.sha256(chunk[header:]).hexdigest()})
    return {'cose_offset': '0x23c5f', 'signers': signers, 'components': components,
            'signature_negative_controls': 4, 'body_mutation_controls': 4,
            'limitations': ['Two independently signed manifests, not a proven certificate chain',
                           'No validation against P16 hardware trust or recovery of plaintext SROOT code']}


def inspect(capsule):
    assert len(capsule) == 30411871
    assert hashlib.sha256(capsule).hexdigest() == CAPSULE_HASH
    packed, body = decode(capsule, PREFIX)
    assert len(packed) == 1037 and body == 0x8d606f
    outer = loads(packed)
    assert len(outer) == 2 and outer[1] == bytes(11)
    assert len(outer[0]) == len(outer[0][0]) == 1
    tagged = outer[0][0][0]
    assert isinstance(tagged, Tag) and tagged.number == 98
    protected, unprotected, payload, signatures = tagged.value
    assert protected == b'' and unprotected == {} and len(signatures) == 1
    signer, unsigned, signature = signatures[0]
    assert unsigned == {} and len(signature) == 384
    headers, claims = loads(signer), loads(payload)
    key = headers[-1277951]
    assert headers[1] == -38 and headers[4] == b'14'
    assert (key[1], key[2], key[3], key[-1310718]) == (3, b'14', -38, 12)
    assert set(key) == {1, 2, 3, -1, -2, -1310718}
    assert int.from_bytes(key[-1], 'big').bit_length() == 3072
    public = der(0x30, integer(key[-1])+integer(key[-2]))
    message = encode(['Signature', protected, signer, b'', payload])
    assert verify(public, signature, message)
    assert not verify(public, bytes([signature[0] ^ 1])+signature[1:], message)
    assert not verify(public, signature, message[:-1]+bytes([message[-1] ^ 1]))
    assert claims[-1310719] == 112
    assert struct.unpack_from('<4I', capsule, body) == (1, 3, 0xefbeadde, 0xefbeadde)
    digests = claims[-1294334]
    assert len(digests) == 3
    expected = [(0, 0, 0, 112), (0x2100, 112, 208, 0x87aa0),
                (0x2200, 0x87be0, 208, 0xc930)]
    rows, extracted = [], {}
    for index, (kind, offset, header, size) in enumerate(expected):
        words = struct.unpack_from('<8I', capsule, body+16+32*index)
        assert words[:5] == (0x10101, kind, offset, header, size)
        assert words[-1] == 0xefbeadde
        start, end = body+offset, body+offset+header+size
        assert 0 <= start < end <= len(capsule)
        chunk = capsule[start:end]
        algorithm, digest = digests[index]
        assert algorithm == -43 and len(digest) == 48
        assert hashlib.sha384(chunk).digest() == digest
        assert hashlib.sha384(chunk[:-1]+bytes([chunk[-1] ^ 1])).digest() != digest
        raw = chunk[header:]
        assert hashlib.sha256(raw).hexdigest() == PAYLOAD_HASHES[index]
        if header:
            assert struct.unpack_from('<4I', chunk) == (1, 208, 0, 0xefbeadde)
            assert chunk[16:24] == b'FW-1.0.0'
            assert struct.unpack_from('<II', chunk, 48) == (0, 0)
            extracted[f'osroot-{kind:04x}.bin'] = raw
        rows.append({'kind': hex(kind), 'descriptor_words': [hex(x) for x in words],
                     'signed_start': hex(start), 'signed_end': hex(end),
                     'payload_start': hex(start+header), 'payload_bytes': len(raw),
                     'payload_sha256': PAYLOAD_HASHES[index]})
    engine = capstone.Cs(capstone.CS_ARCH_RISCV, capstone.CS_MODE_RISCV32 | capstone.CS_MODE_RISCVC)
    vectors = list(engine.disasm(extracted['osroot-2100.bin'][:128], 0))
    assert len(vectors) == 32 and all(i.mnemonic == 'j' and i.size == 4 for i in vectors)
    # Offsets only: neither file starts at an established CPU virtual address.
    terms = (b'OSROT FW', b'rot_mailbox_service_request()', b'Auth Result',
             b'/proj/mtk22036/n1x/RoT_NativeSDK_0523_osroot_wagyu/')
    strings = {term.decode(): extracted['osroot-2200.bin'].find(term) for term in terms}
    assert all(offset >= 0 for offset in strings.values())
    code, data = extracted['osroot-2100.bin'], extracted['osroot-2200.bin']
    # Independent references constrain the text base, rather than treating
    # descriptor transport addresses as CPU execution addresses.
    startup = [(i.mnemonic, i.op_str) for i in engine.disasm(code[0xf0:0xf8], 0xf0)]
    assert startup == [('auipc', 't0, 0x168'), ('addi', 't0, t0, -0x84')]
    text_base = 0x17d06c - (0xf0 + 0x168000 - 0x84)
    assert text_base == 0x15000
    scrub = [(i.mnemonic, i.op_str) for i in engine.disasm(code[0x87a4e:0x87a60], 0x87a4e)]
    assert scrub == [('c.lui', 'a5, 0x15'), ('mv', 'a5, a5'),
                     ('lui', 'a3, 0x9d'), ('addi', 'a4, a3, -0x600'),
                     ('bltu', 'a5, a4, 0x2e')]
    assert text_base+0x87a00 == 0x9d000-0x600
    services = []
    for mailbox in range(4):
        table = struct.unpack_from('<I', data, 0x4010+4*mailbox)[0]
        count = data[0x40e8+mailbox]
        for index in range(count):
            command, target, mask, flag = struct.unpack_from('<4I', data,
                                                            table-0x144000+16*index)
            offset = target-text_base
            assert 0 <= offset < 0x13000 and mask == 31 and flag == 1
            first = next(engine.disasm(code[offset:offset+4], offset))
            assert first.mnemonic in ('c.addi16sp', 'c.addi', 'c.beqz')
            services.append({'mailbox': mailbox, 'command': hex(command),
                             'handler_address': hex(target), 'handler_offset': hex(offset)})
    assert len(services) == 10
    return {'passed': True, 'hardware_access': False, 'capsule_sha256': CAPSULE_HASH,
            'cose_offset': hex(PREFIX), 'algorithm': 'PS384', 'key_id': '14',
            'embedded_key_signature_verified': True, 'signature_negative_controls': 2,
            'body_digest_negative_controls': 3, 'components': rows,
            'riscv_vector_jump_targets_relative': [i.op_str for i in vectors],
            'data_string_offsets': strings,
            'text_base_evidence': {'base': hex(text_base), 'startup_reference_offset': '0xf0',
                                  'cookie_address': '0x17d06c', 'scrub_end': '0x9ca00'},
            'static_services': services,
            'sroot_fw_reference': inspect_sroot(capsule),
            'limitations': ['DGX Spark research input, not a P16 firmware update',
                'Embedded-key verification does not validate the capsule certificate chain or P16 trust',
                'Descriptor address words are preserved without assigning a CPU memory map',
                'OSROOT identity/version equivalence to the P16 and SROOT is not established',
                'No firmware error meanings inferred and no device access']}, extracted


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extract-dir', type=Path,
                        help='Create a new directory for the two verified raw research components')
    args = parser.parse_args()
    report, blobs = inspect((ROOT/'sources/dgx-spark-socfw-2.158.15/socfw.cap').read_bytes())
    if args.extract_dir:
        args.extract_dir.mkdir()  # Refuse an existing directory; never overwrite artifacts.
        for name, data in blobs.items():
            with (args.extract_dir/name).open('xb') as stream:
                stream.write(data)
    print(json.dumps(report, indent=2))
