#!/usr/bin/env python3
"""Replay the GSP's VBIOS-labelled blob exporter and its MXM input boundary.

Offline only. Allocation/free, logging and the NOCAT publisher are stubs;
the producer additionally stubs registry/property/platform reads. The observed
producer supplies MXM data, not a demonstrated complete board ROM. This does
not establish that a Linux diagnostic request reaches the exporter.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import Uc, UC_ARCH_RISCV, UC_HOOK_CODE, UC_MODE_RISCV64
from unicorn.riscv_const import (
    UC_RISCV_REG_A0, UC_RISCV_REG_A1, UC_RISCV_REG_A2,
    UC_RISCV_REG_A3, UC_RISCV_REG_A4,
    UC_RISCV_REG_PC, UC_RISCV_REG_RA, UC_RISCV_REG_SP,
)

ELF_SHA256 = "eb092d6d6f156e1403b479bcdc33c6f0d7470ad20f259b9c97ff3e06ef80eb5c"
ENTRY = 0x1BD0148
ALLOC = 0x18E2D78
FREE = 0x18E17E4
LOG = 0x1AD7214
PUBLISH = 0x1C1B784
STOP = 0x7000000
GPU, OBJ, CTX, META = 0x6000000, 0x6003000, 0x6004000, 0x6005000
BLOB, HEAP, HEAP_END = 0x8000000, 0x8200000, 0x8600000


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, payload):
    return varint(number * 8 + 2) + varint(len(payload)) + payload


def machine(binary):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    # The two PT_LOAD segments of the hash-pinned ELF, with no relocation.
    for va, offset, length in ((0x1000000, 0, 0xED2000),
                               (0x4000000, 0xED2000, 0x1AF000)):
        u.mem_map(va, length)
        u.mem_write(va, binary[offset:offset + length])
    u.mem_map(GPU, 0x10000)
    u.mem_map(STOP, 0x10000)
    u.mem_map(BLOB, HEAP_END - BLOB)
    return u


def replay(binary, size, *, enabled=True, present=True, fail_alloc=0):
    u = machine(binary)

    def put64(address, value):
        u.mem_write(address, struct.pack("<Q", value))

    def get64(address):
        return struct.unpack("<Q", u.mem_read(address, 8))[0]

    def get32(address):
        return struct.unpack("<I", u.mem_read(address, 4))[0]

    # All byte values, with a recognizable synthetic prefix; never a board ROM.
    pattern = b"\x55\xaaSYNTHETIC-VBIOS" + bytes(range(256))
    payload = (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]
    if payload:
        u.mem_write(BLOB, payload)
    u.mem_write(GPU + 0x72E, bytes([enabled]))
    put64(GPU + 0x2098, META)
    put64(META + 0x14C, 123)
    put64(OBJ + 0x630, BLOB if present else 0)
    put64(OBJ + 0x638, size)
    for reg, value in ((UC_RISCV_REG_A0, GPU), (UC_RISCV_REG_A1, OBJ),
                       (UC_RISCV_REG_A2, CTX), (UC_RISCV_REG_SP, STOP + 0xF000),
                       (UC_RISCV_REG_RA, STOP)):
        u.reg_write(reg, value)

    allocations, publications, logs = [], [], []
    next_heap = HEAP

    def stub_return(value=0):
        u.reg_write(UC_RISCV_REG_A0, value)
        u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    def hook(uc, pc, instruction_size, user_data):
        nonlocal next_heap
        if pc == ALLOC:
            length = u.reg_read(UC_RISCV_REG_A1)
            allocations.append(length)
            if len(allocations) == fail_alloc:
                stub_return(0)
            else:
                address = next_heap
                next_heap += (length + 15) & ~15
                if not 0 < length <= 0x110020 or next_heap > HEAP_END:
                    raise RuntimeError("Unexpected allocation")
                stub_return(address)
        elif pc == FREE:
            stub_return()
        elif pc == LOG:
            logs.append(u.reg_read(UC_RISCV_REG_A1))
            stub_return()
        elif pc == PUBLISH:
            desc = u.reg_read(UC_RISCV_REG_A1)
            name = bytes(u.mem_read(get64(desc + 16), 6))
            if name != b"VBIOS\0":
                raise RuntimeError("Unexpected publisher source")
            publications.append({"record_type": u.mem_read(desc + 8, 1)[0],
                                 "metadata": get64(desc + 32),
                                 "bytes": get32(desc + 40),
                                 "pointer": get64(desc + 48)})
            stub_return()

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(ENTRY, STOP, count=5_000_000)
    if u.reg_read(UC_RISCV_REG_PC) != STOP:
        raise RuntimeError("Exporter did not return within the instruction bound")
    node = get64(CTX + 8)
    encoded = b""
    if node:
        address, length = get64(node), get32(node + 8)
        if not HEAP <= address < HEAP_END or length > size + 32:
            raise RuntimeError("Invalid output extent")
        if get64(node + 16):
            raise RuntimeError("Unexpected second list node")
        encoded = bytes(u.mem_read(address, length))

    if not enabled or not present:
        assert not allocations and not publications and not node
    elif fail_alloc:
        assert not node
        if fail_alloc == 1:
            assert logs and not publications
        else:
            # Node-allocation failure silently discards the encoded buffer,
            # then publishes an empty descriptor. A call is not export success.
            assert not logs
            assert publications == [{"record_type": 2, "metadata": 123,
                                     "bytes": 0, "pointer": 0}]
    else:
        expected = field(307, field(1, payload))
        assert encoded == expected, (size, len(encoded), len(expected))
        assert allocations == [size + 32, 24]
        assert not logs
        assert publications == [{"record_type": 2, "metadata": 123,
                                 "bytes": len(encoded), "pointer": get64(node)}]
    return {"input_bytes": size, "enabled": enabled, "present": present,
            "fail_allocation": fail_alloc, "allocation_sizes": allocations,
            "encoded_bytes": len(encoded), "log_calls": len(logs),
            "publisher_calls": len(publications), "passed": True}


def replay_mxm_source(binary, size, *, property_source=False,
                      disabled=False, corrupt_checksum=False):
    """Run the original producer prefix, stopping before descriptor decoding.

    This tests source selection, the 4096-byte limit and checksum, not the
    subsequent MXM descriptor parser or a real platform read.
    """
    u = machine(binary)
    # Synthetic version-3 MXM header plus opaque bytes, not a valid full SIS.
    data = bytearray(b"MXM_\x03\x00" + struct.pack("<H", size - 8))
    data.extend(bytes(size - len(data)))
    data[-1] = (-sum(data)) & 255
    if corrupt_checksum:
        data[-1] ^= 1
    u.mem_write(GPU + 0x74E, b"\1")
    u.mem_write(OBJ + 0xF20, struct.pack("<I", 0x30))
    for reg, value in ((UC_RISCV_REG_A0, GPU), (UC_RISCV_REG_A1, OBJ),
                       (UC_RISCV_REG_A2, CTX), (UC_RISCV_REG_A3, META),
                       (UC_RISCV_REG_A4, 0), (UC_RISCV_REG_SP, STOP + 0xF000),
                       (UC_RISCV_REG_RA, STOP)):
        u.reg_write(reg, value)
    properties, registry, reads, allocations = [], [], [], []
    accepted = False

    def stub_return(value=0):
        u.reg_write(UC_RISCV_REG_A0, value)
        u.reg_write(UC_RISCV_REG_PC, u.reg_read(UC_RISCV_REG_RA))

    def cstring(address):
        return bytes(u.mem_read(address, 64)).split(b"\0")[0].decode()

    def hook(uc, pc, instruction_size, user_data):
        nonlocal accepted
        if pc == 0x1180F0A:
            accepted = True
            u.emu_stop()
        elif pc in (LOG, 0x1BA0824, 0x1AC846C, 0x18E18D8):
            stub_return()
        elif pc == 0x103F1D8:
            name = cstring(u.reg_read(UC_RISCV_REG_A1))
            registry.append(name)
            if name == "MXMDisable" and disabled:
                u.mem_write(u.reg_read(UC_RISCV_REG_A2), struct.pack("<I", 1))
                stub_return()
            else:
                stub_return(1)
        elif pc == 0x18E32EC:
            length = u.reg_read(UC_RISCV_REG_A0)
            assert length == 4096
            allocations.append(length)
            stub_return(BLOB)
        elif pc == 0x1B8C14C:
            name = cstring(u.reg_read(UC_RISCV_REG_A0))
            properties.append(name)
            if property_source:
                value = {"MXMVersion": struct.pack("<I", 0x30),
                         "MXMSize": bytes([size]), "MXMData": bytes(data)}[name]
                u.mem_write(u.reg_read(UC_RISCV_REG_A1), value)
                u.mem_write(u.reg_read(UC_RISCV_REG_A2), struct.pack("<I", len(value)))
                stub_return()
            else:
                stub_return(1)
        elif pc == 0x1B1D290:
            length = u.reg_read(UC_RISCV_REG_A1)
            reads.append(length)
            assert length in (8, size) and length <= 4096
            u.mem_write(u.reg_read(UC_RISCV_REG_A2), bytes(data[:length]))
            stub_return()

    u.hook_add(UC_HOOK_CODE, hook)
    u.emu_start(0x1180988, STOP, count=200_000)
    pc = u.reg_read(UC_RISCV_REG_PC)
    assert pc in (STOP, 0x1180F0A), hex(pc)
    assert accepted == (not disabled and size <= 4096 and not corrupt_checksum)
    assert registry == ["MXMDisable"]
    if disabled:
        assert not properties and not reads and not allocations
    elif property_source:
        assert properties == ["MXMVersion", "MXMSize", "MXMData"] and not reads
    else:
        assert properties == ["MXMVersion"]
        assert reads == ([8, size] if size <= 4096 else [8])
    if not disabled:
        assert allocations == [4096]
    # Prefix alone has not populated the export object or enabled its gate.
    assert bytes(u.mem_read(OBJ + 0x630, 12)) == bytes(12)
    assert u.mem_read(GPU + 0x72E, 1) == b"\0"
    return {"input_bytes": size, "source": "properties" if property_source else "platform",
            "disabled": disabled, "corrupt_checksum": corrupt_checksum,
            "properties": properties, "platform_read_sizes": reads,
            "reached_descriptor_parser": accepted, "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True,
                        help="Locally extracted 615.78.08 GSP RM RV64 ELF")
    parser.add_argument("--output", type=Path,
                        default=Path("results/gsp-vbios-export.json"))
    args = parser.parse_args()
    binary = args.elf.read_bytes()
    if hashlib.sha256(binary).hexdigest() != ELF_SHA256:
        parser.error("GSP ELF hash mismatch; these addresses require the pinned build")
    cases = [replay(binary, n) for n in (0, 1, 127, 128, 1024, 4096, 0x110000)]
    cases += [replay(binary, 4096, enabled=False),
              replay(binary, 4096, present=False),
              replay(binary, 4096, fail_alloc=1),
              replay(binary, 4096, fail_alloc=2)]
    producer_cases = [replay_mxm_source(binary, n) for n in (16, 4096, 4097)]
    producer_cases += [replay_mxm_source(binary, 16, property_source=True),
                      replay_mxm_source(binary, 16, disabled=True),
                      replay_mxm_source(binary, 16, corrupt_checksum=True)]
    result = {"elf_sha256": ELF_SHA256, "entry_va": hex(ENTRY),
              "protobuf_path": [307, 1],
              "scope": "Original exporter and encoder; allocation/free, logging and "
                       "NOCAT publisher stubbed. No Linux request dispatch or hardware.",
              "cases": cases,
              "producer_scope": "Original 0x1180988 prefix through source/size/checksum "
                                "gates; platform and property getters stubbed; stop before "
                                "MXM descriptor decoding. Not a full ROM producer.",
              "producer_cases": producer_cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS: {len(cases)} labelled-blob export and {len(producer_cases)} MXM source cases (offline)")


if __name__ == "__main__":
    main()
