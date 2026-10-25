#!/usr/bin/env python3
"""Read the pinned NVIDIA module's cached FRTS metadata through /proc/kcore.

Reads kernel RAM only: no GPU RPC, physical-memory access, MMIO, mapping,
module loading or firmware upload. Requires root. Does not acquire ROM bytes.
The cache offsets are validated against the running module's instructions.
"""
import hashlib
import json
import os
import struct
from pathlib import Path

KERNEL = "7.2.8-23-omarchy-p16-asus-npu1"
VERSION = "615.78.08"
SRCVERSION = "4200B1C66D4D12038311105"
MODULE = Path(f"/lib/modules/{KERNEL}/updates/npu-test/nvidia.ko")
MODULE_SHA256 = "d8878935a9c353dc82cc7b31f85a8a1e9eac6d0dab624a03451fc3616f56da1c"
# Non-relocated AArch64 instructions in the installed module. Addresses below
# are relative to their named functions, not hardcoded live kernel addresses.
CHECKS = {
    "gpumgrGetGpu": {0x24: 0xF940F413, 0x28: 0x91412275, 0x2C: 0x91412273,
                     0x30: 0x911D8273, 0x34: 0xB94962A0, 0x48: 0x91004273,
                     0x54: 0xB9400A60, 0x60: 0xF9400277, 0x78: 0x395296E0},
    "gpuGetGspStaticInfo": {0: 0x39664801, 8: 0xF94E1803,
                            0x10: 0x9141A863, 0x14: 0x9100A063},
    "kgspPopulateSrRegionsInfo_GH100": {
        0x18: 0xF9457034, 0x34: 0xF9405281, 0x38: 0xF9058A61,
        0x48: 0x395342A1, 0x50: 0xF9431801, 0x54: 0xF9057A61,
        0x58: 0xF9431C00, 0x5C: 0xF9058660},
}


def pointer(value):
    if value < 0xFFFF000000000000 or value & 7:
        raise ValueError(f"Not an aligned kernel pointer: {value:#x}")
    return value


class CoreReader:
    def __init__(self, fd):
        self.fd = fd
        hdr = os.pread(fd, 64, 0)
        if len(hdr) != 64 or hdr[:6] != b"\x7fELF\x02\x01":
            raise ValueError("Expected a little-endian ELF64 kernel core")
        if struct.unpack_from("<HH", hdr, 16) != (4, 183):
            raise ValueError("Expected an AArch64 ET_CORE file")
        phoff = struct.unpack_from("<Q", hdr, 32)[0]
        phsize, phnum = struct.unpack_from("<HH", hdr, 54)
        if phsize != 56 or not 0 < phnum <= 4096:
            raise ValueError("Unexpected kernel core program headers")
        headers = os.pread(fd, phsize * phnum, phoff)
        if len(headers) != phsize * phnum:
            raise ValueError("Truncated kernel core program headers")
        self.segments = []
        for h in struct.iter_unpack("<IIQQQQQQ", headers):
            kind, flags, offset, va, pa, filesz, memsz, align = h
            if kind == 1 and filesz:
                self.segments.append((va, min(filesz, memsz), offset))

    def read(self, address, size):
        if not 0 < size <= 256 or not 0 <= address < (1 << 64) - size:
            raise ValueError("Out-of-bounds kernel read")
        matches = [(va, off) for va, length, off in self.segments
                   if va <= address and address + size <= va + length]
        if len(matches) != 1:
            raise ValueError("Kernel read is not inside one unique file-backed segment")
        va, off = matches[0]
        data = os.pread(self.fd, size, off + address - va)
        if len(data) != size:
            raise ValueError("Short kernel read")
        return data

    def integer(self, address, size=8):
        return int.from_bytes(self.read(address, size), "little")


def inspect_cache(core, symbols):
    for name, instructions in CHECKS.items():
        start = pointer(symbols[name])
        for offset, word in instructions.items():
            if core.read(start + offset, 4) != struct.pack("<I", word):
                raise ValueError(f"Running instruction differs: {name}+{offset:#x}")
    system = pointer(core.integer(pointer(symbols["g_pSys"])))
    manager = pointer(core.integer(system + 0x1E8))
    count = core.integer(manager + 0x48960, 4)
    if count != 1:
        raise ValueError(f"Expected exactly one GPU handle, found {count}")
    entry = manager + 0x48760
    gpu = pointer(core.integer(entry))
    instance = core.integer(entry + 8, 4)
    if not core.integer(gpu + 0x4A5, 1):
        raise ValueError("GPU is not initialized")
    if core.integer(gpu + 0x992, 1):
        raise ValueError("GPU uses the alternate static-info path")
    if not core.integer(gpu + 0x4D0, 1):
        raise ValueError("GPU does not use the ACR-provided WPR offsets")
    kgsp = pointer(core.integer(gpu + 0x1C30))
    static = kgsp + 0x6A028
    offset = core.integer(static + 0x638)
    sr_offset = core.integer(kgsp + 0xB08)
    size = core.integer(kgsp + 0xB10)
    wpr = pointer(core.integer(kgsp + 0xAE0))
    host_size = core.integer(wpr + 0xA0)
    if offset != sr_offset or size != host_size:
        raise ValueError("Cached FRTS fields disagree; no coherent snapshot")
    return {"gpu_instance": instance, "frts_offset": hex(offset),
            "frts_size": size, "static_and_suspend_cache_agree": True,
            "meaning": "Firmware-region offset, not a verified CPU physical address; "
                       "zero fields do not establish an available FRTS container."}


def main():
    if os.uname().release != KERNEL:
        raise ValueError("Running kernel differs from the pinned build")
    for filename, expected in (("version", VERSION), ("srcversion", SRCVERSION)):
        if Path(f"/sys/module/nvidia/{filename}").read_text().strip() != expected:
            raise ValueError(f"Running NVIDIA {filename} differs")
    if hashlib.sha256(MODULE.read_bytes()).hexdigest() != MODULE_SHA256:
        raise ValueError("Pinned module file hash differs")
    wanted = set(CHECKS) | {"g_pSys"}
    symbols = {}
    for line in Path("/proc/kallsyms").read_text().splitlines():
        words = line.split()
        if len(words) == 4 and words[2] in wanted and words[3] == "[nvidia]":
            if words[2] in symbols:
                raise ValueError("Duplicate NVIDIA symbol")
            symbols[words[2]] = int(words[0], 16)
    if set(symbols) != wanted or not all(symbols.values()):
        raise ValueError("NVIDIA kernel symbols unavailable; root access is required")
    fd = os.open("/proc/kcore", os.O_RDONLY | os.O_CLOEXEC)
    try:
        core = CoreReader(fd)
        result = inspect_cache(core, symbols)
        if inspect_cache(core, symbols) != result:
            raise ValueError("Cache changed during inspection")
    finally:
        os.close(fd)
    print(json.dumps({"kernel": KERNEL, "nvidia": VERSION,
                      "module_sha256": MODULE_SHA256, **result}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Cannot read cached FRTS metadata: {exc}") from exc
