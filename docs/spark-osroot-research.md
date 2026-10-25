# DGX Spark OSROOT as an offline protocol reference

Both physical P16 upload variants returned nonzero SROOT replies. The Windows
provider and KMD transport preserve no semantic decoder for their status/detail
fields. A separate manufacturer firmware package now supplies readable OSROOT
code for investigating the shared mailbox/authentication machinery. It is an
**offline reference**, not firmware to install on the ASUS P16.

## Source and integrity

The [LVFS public catalog](https://cdn.fwupd.org/downloads/firmware.xml.gz)
advertises NVIDIA component `com.nvidia.dgx.spark.socfw.firmware`, release
`152274`, version `0x02009e0f` (2.158.15), dated 2026-09-28. NVIDIA's
[DGX Spark release notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)
also list SoC 2.158.15. The advertised capsule target GUID is
`b488217b-3895-4fc0-b1bf-ab7005a2d45a`; this has not been matched to the P16.

| Artifact | Bytes | SHA-256 |
| --- | --- | --- |
| socfw.cab | 30443174 | f895952896ffed39fc167de0f86eab40609b5baf9debcf12caa720123a915416 |
| socfw.cap | 30411871 | 00393be197569851c9a72bd214cf22f45a04b7e1fc759ab4ccc68c98dd650d78 |

Both downloaded/extracted hashes match the catalog. The catalog and package
were fetched over HTTPS; the LVFS metadata signature and outer UEFI capsule
certificate chain were **not** validated by this analysis. There was no fwupd
installation, capsule staging, reboot request or hardware write.

The ASUS H7607BA BIOS API returned `FAIL` with a null result during this query;
the model's driver API returned 20 files without a BIOS/SoC package. This is
not proof that ASUS firmware is unavailable elsewhere. The existing recovery
WIM contains `nvsrootmbx.sys`, but its filename inventory exposed no separate
SROOT/OSROOT firmware image. P16 ESRT entry reads required root and were not
performed; no firmware identity was inferred from the entries' mere presence.

## Verified OSROOT envelope and components

At capsule offset `0x8d5c5f`, a CBOR byte string contains a COSE_Sign object.
The signed body starts at `0x8d606f`. The signer uses algorithm `-38` (PS384),
key ID ASCII `14` and an embedded 3072-bit RSA public key. Its signature verifies
with OpenSSL using SHA-384, MGF1-SHA-384 and a 48-byte PSS salt, as specified by
[RFC 8230](https://www.rfc-editor.org/rfc/rfc8230.html#section-2). Mutating either
the signature or message fails verification. This is mathematical consistency
with the embedded key, not proof that the P16 trusts it.

The signed manifest's three SHA-384 digests match the descriptor block and two
complete components. Their boundaries come from the signed 112-byte descriptor
block, rather than string searches alone. Each nonzero component has a 208-byte
wrapper labeled `FW-1.0.0`, with words at wrapper offsets 48/52 equal to zero.

| Kind field | Signed capsule extent (end exclusive) | Payload start | Payload bytes | Payload SHA-256 |
| --- | --- | --- | --- | --- |
| `0` | `0x8d606f..0x8d60df` | `0x8d606f` | 112 | 411558877583f837f6c0ba2b2e1df42122296067d9ae5cc83906bd915db0dce2 |
| `0x2100` | `0x8d60df..0x95dc4f` | `0x8d61af` | 555680 | 08adaedac470ec367e74a644663ab68b92023fc47178aaec896f2a1ae468eb47 |
| `0x2200` | `0x95dc4f..0x96a64f` | `0x95dd1f` | 51504 | 1086a49e17844e2aad6990a70f1c25a979ecf08f09991e8ce7f203da8791b7d8 |

The `0x2100` payload begins with 32 valid RISC-V jump instructions and contains
RV32 compressed code. The `0x2200` payload starts with `OSROT FW` and contains
mailbox, authentication and error-handler strings, including a source path
under `n1x/RoT_NativeSDK_0523_osroot_wagyu/`. These identify an OSROOT research
candidate; they do not establish P16/SROOT firmware version or ABI equality.

An initial cross-reference supports a data-address hypothesis of `0x144000`:
code offsets `0x4168..0x4186` construct `0x144648`, `0x146d54` and `0x1480c4`;
subtracting that base locates an IRQ synchronization error format, `[ERROR]`
and newline in the second payload. Adjacent code also constructs `0x144630`
and `0x14466c`, matching nearby error formats. This is static evidence only.
The CPU code base, complete mapping and entry path still require reconstruction
before a full firmware emulation can be treated as reliable. The bounded reply
routine below can nevertheless run with a synthetic code base because its
executed calls are PC-relative and its absolute data accesses are accounted for.
Descriptor words `0x10001000`/`0x10001100` are preserved without labeling them
CPU execution addresses.

The capsule also contains labeled SROOT_FMC and SROOT_FW components with
different wrapper flags and high-entropy payload regions. No transform or
decryption interpretation is established. Do not equate OSROOT helper return
values with the P16 SROOT replies `6/42` or `1023/37` without tracing the actual
packing and error-return paths. Those reply meanings remain unresolved.

## Original reply routine: verified field packing

`scripts/test_osroot_reply.py` executes the original RV32 routine at payload
offset `0x341c`, its original register-address accessor at `0x31c0` and the
original zero-return helper at `0x36b6`. There are no function stubs. The harness
maps code at synthetic address zero, supplies stack and cookie memory, and
replaces the four register bases at `0x146d44` with emulator-owned memory.
This is function-level execution, not a recovered full boot layout.

The input structure contains a 32-bit combined status at offset 0, command byte
at offset 4, low-four-bit payload count at offset 8 and payload words at offset
12. The routine writes its third argument to mailbox offset `0x210`, emits the
header and every payload word through the same offset `0x214`, then writes 1 to
`0x21c`. The header is:

```text
command | (count << 8) | ((status & 0xfff) << 12)
        | (((status >> 12) & 0xff) << 24)
```

All 80 synthetic combinations of four mailboxes, five counts and four statuses
produce the expected complete write sequence; null-packet and invalid-mailbox
controls return 1 without register writes. The two observed P16 replies are
reproduced from combined statuses `0x2a006` and `0x253ff`, then decoded by the
original Windows provider to `(command=0x18, status=6, detail=42, count=0)` and
`(command=0x18, status=1023, detail=37, count=0)` respectively.

This proves shared field packing, not shared error semantics, successful image
authentication or that OSROOT is the P16 SROOT image. The reference routine
accepts a count of 15; Linux's existing maximum of 14 is unchanged. The static
service tables referenced by the dispatcher at offset `0x1186a` list commands
`0x10/0x19/0x30/0x31` for mailbox 0, none for mailbox 1,
`0x1b/0x22/0x23/0x25` for mailbox 2, and `0x02/0x03` for mailbox 3.
They do not supply a handler for upload command `0x18`. Their code-pointer
mapping and the actual SROOT upload error producers remain unresolved.

## Reproduce without touching hardware

Download the catalog-pinned package from the LVFS CDN, validate its hash, and
unpack it with the existing local 7-Zip tool:

```sh
curl --fail --location --output sources/dgx-spark-socfw-2.158.15.cab \
  https://cdn.fwupd.org/downloads/f895952896ffed39fc167de0f86eab40609b5baf9debcf12caa720123a915416-socfw.cab
printf '%s  %s\n' \
  f895952896ffed39fc167de0f86eab40609b5baf9debcf12caa720123a915416 \
  sources/dgx-spark-socfw-2.158.15.cab | sha256sum -c -
tools/usr/lib/7zip/7z x -osources/dgx-spark-socfw-2.158.15 \
  sources/dgx-spark-socfw-2.158.15.cab
venv/bin/python scripts/inspect_spark_osroot.py
# Optional; refuses an existing directory instead of overwriting prior evidence:
venv/bin/python scripts/inspect_spark_osroot.py --extract-dir sources/dgx-spark-osroot
# After extraction; also needs the existing pinned Windows provider:
venv/bin/python scripts/test_osroot_reply.py
```

The script pins the whole capsule, verifies the internal signature and all
three component digests, runs two signature and three body-mutation controls,
and checks the RISC-V vector/string evidence. It uses existing Capstone/OpenSSL
dependencies. Default mode writes only temporary verification files; extraction
creates two ignored local research binaries. No vendor binaries enter Git.
