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

## Windows fallback contains the complete raw HAL-1 firmware

The statement above concerns **variant 0**. Comparing all four vendor-signed
uploads establishes a stronger result for the Windows fallback, **remapper 1,
variant 1**: its bytes `[692:67508]` equal the complete 66816-byte raw HAL-1
image, SHA-256 `2754b289e39b8d9ae9383d952f738bd1c5027fdd2573a564f664f9f66ac2f997`.
This is a whole-image byte comparison, including the raw header, code, data
and padding. The vendor input is unmodified; no decryption, re-signing or
authentication bypass is involved.

The fallback's whole-image SHA-256 is
`6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4`, at KMD
RVA `0x13f5040`. Its layout is the same 404-byte prefix, 80-byte first body
component and 208-byte wrapper preceding the raw image. The wrapper's LE u32
at relative offset 4 is 208; its bytes at relative offset 16 contain `DLA\0`.
These observations locate the raw image without inferring undocumented flags.

`inspect_sroot_variants.py` verifies all four signatures against their embedded
public keys and both body digests for every image. Variant 0 and variant 1
use different P-384 keys despite both key IDs being ASCII `08`:

| Variant | Public-key SPKI SHA-256 |
| --- | --- |
| 0 | dd3164c1183db88e20bd69a5695a2b336584b53981d6e8d3175b4388f36a96a3 |
| 1 | c2bca727f9be28f280b173fd4de63d58ad27264963169918810997f89d874a9f |

Each key is reused across the two remapper groups. Four cross-key verification
controls fail as expected. Eight body-mutation controls detect changed data.
Within either remapper group, the first 80-byte component is identical between
variants, while the second component differs. Wrapper words at relative
offsets 48/52 are `(1, 3)` for variant 0 and `(0, 0)` for variant 1. No
production/debug classification, cipher or rollback meaning is assigned to
these fields or the changed vendor claims.

Original KMD selector `0xd45cb0`, exercised by `emulate_boot_variants.py`, tries
variant 1 after an initial variant-0 request fails and no previous success is
recorded. That is a general request-failure fallback, not a special decoder
for status 6/detail 42. After a previous success it does not switch variants
on failure. The Linux experiment deliberately retains one-attempt semantics;
it requires a fresh boot and explicit variant selection instead of replaying
Windows' DMA-free/retry sequence.

This proves that a vendor-signed, Windows-selectable upload contains the raw
firmware used by our scheduler tests. It does **not** prove that this machine
trusts its embedded key, accepts it, executes it, or performs inference.
Variant 0's transformation and the first physical upload error remain unknown.
The [first variant-1 hardware attempt](linux-backend.md#earlier-npu1-hardware-result-variant-1-rejected)
has since returned `0x253ff018` (status `1023`, detail `37`) and was rejected.
Its mathematical signature validity and raw-image equality did not establish
hardware acceptance. The new error's meaning remains unresolved.

Later hardware result: after adding the actual board's HFRP setup/power
prelude, **variant 0 was accepted and executed**, including DMA ping and
bypass-task completion. See [the physical record](p16-board-start.md).
This does not identify the transformation of its opaque payload or decode the
earlier errors, and variant 1 has not been retested after that prelude.

```sh
venv/bin/python scripts/inspect_sroot_variants.py
venv/bin/python scripts/emulate_boot_variants.py
venv/bin/python scripts/test_sroot_full_upload.py
```

```sh
venv/bin/python scripts/inspect_sroot_envelope.py
```

The tool only reads ignored vendor inputs and uses temporary public-key,
signature and message files for OpenSSL. It writes no device register, changes
no boot entry and installs/loads no module or firmware.
