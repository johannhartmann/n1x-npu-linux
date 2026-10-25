# Signed SROOT envelope: offline structure and integrity evidence

The exact default P16 SROOT input is 67508 bytes, SHA-256
`0f16911cced7bb95773da5aff9d9a44dfd1b1a11f3c560ee698a5c99310ff2c4`.
Its first LE u32 is the already recovered mailbox image identifier 0xc0302.
The following bytes are not uniformly opaque: a definite-length CBOR byte
string encodes a signed envelope. No device access or authentication bypass
is involved in this analysis.

## Recovered structure

| File extent | Observed content |
| --- | --- |
| 0x000..0x003 | LE image identifier 0xc0302 |
| 0x004..0x006 | CBOR byte-string header, length 397 |
| 0x007..0x193 | Nested arrays containing COSE_Sign tag 98 and a separate six-zero-byte field |
| 0x194..0x1e3 | First body component, 80 bytes |
| 0x1e4..0x107b3 | Second body component, 67024 bytes |

The nested tagged object has the standard COSE_Sign four-element shape:
empty protected bytes, empty unprotected map, a 144-byte payload and one signer.
That signer contains a 133-byte protected map, an empty unprotected map and
96 signature bytes. Tag 98 and the signature structure correspond to
[RFC 9052, signing and verification](https://www.rfc-editor.org/rfc/rfc9052.html#section-4.4).

The protected signer map identifies algorithm -35 and key ID bytes `30 38`
(ASCII 08). Vendor key -1277951 contains a COSE EC2 public key with algorithm
-35, curve 2 and two 48-byte coordinates. Those standard values correspond to
ES384/P-384 under [RFC 9053](https://www.rfc-editor.org/rfc/rfc9053.html#section-2.1).
No private key or decryption material is recovered or needed for verification.

## Signature and covered body bytes

`scripts/inspect_sroot_envelope.py` constructs the exact COSE Sig_structure:

```text
["Signature", body_protected, signer_protected, empty_external_aad, payload]
```

OpenSSL verifies the signature against the public key embedded in the protected
signer data. The script derives a normal EC SubjectPublicKeyInfo and converts
COSE's fixed-width r/s into an ASN.1 ECDSA signature for OpenSSL. The separate
six-zero-byte field is **not** used as external AAD: using it makes verification
fail. Its actual vendor meaning remains unknown.

The signed payload contains vendor key -1310719 with value **80** and key
-1294334 with two `[-43, 48-byte digest]` entries. The actual SHA-384 values
match exactly:

| Digest entry | Covered file bytes | SHA-384 |
| --- | --- | --- |
| 0 | 0x194..0x1e3 | 0c77c84b2a677766d6d4a98271a48dee42b1e71af9bd90c24875a3fa54acdcd432b48fbe147dd64ffda7c5935f911b06 |
| 1 | 0x1e4..0x107b3 | f7ed2dc0d3537ef831f4f82f6abdb32d208f267c3336cbf9df41537a8dd894ba5c4f9be763917e377886371ef736ff12 |

Thus all bytes after the CBOR prefix are covered by those two manifest digests.
The first component's length is taken from the signed field, rather than
assuming that a visually plausible header boundary is correct. Other vendor
claim values are preserved without assigning platform/version/time semantics.

Controls reject a changed signature, changed signed payload and nonempty
external AAD, and detect modifications in either body component. Eight bounded
CBOR error/truncation/duplicate-key/extent controls also pass. The parser is a
small analysis-only definite-length subset for this pinned input, not a general
COSE library, production authenticator or kernel parser.

## What this does and does not establish

The signature proves mathematical consistency with the **embedded** public key.
It does not establish that the chip trusts that key, a vendor certificate chain,
anti-rollback policy, firmware transport acceptance or successful hardware
execution. These remain separate qualification requirements. The production
bootstrap still uses its existing exact whole-image SHA-256 check; no new
kernel crypto parser or mutable trust policy is added.

The signed digest values do not equal SHA-384 of the separate raw HAL-1 file,
its payload, code or data. Sample code/data sequences are not present as direct
byte strings in the SROOT image. This does **not** prove different executable
semantics: an internal transform could still encapsulate the raw image. Nor do
high entropy and header labels alone prove an encryption/compression algorithm.
The identity of code actually executed after SROOT loading remains unproved.
Physical boot plus ABI qualification remains essential before applying the raw
firmware emulation findings to this exact hardware image.

```sh
venv/bin/python scripts/inspect_sroot_envelope.py
```

The tool only reads ignored vendor inputs and uses temporary public-key,
signature and message files for OpenSSL. It writes no device register, changes
no boot entry and installs/loads no module or firmware.
