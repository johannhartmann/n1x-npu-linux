#!/usr/bin/env python3
"""Capture one bounded GFWI candidate from translated System RAM via kcore.

Load n1x_gpu_iova_observe immediately before running this helper and reload it
afterwards to compare cpu_pages with the reported page list. The module's
parameters are a snapshot, not a lock on GPU mappings. This helper never loads
modules, maps physical memory, sends RPCs or accesses device registers.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct

KERNEL = "7.2.8-23-omarchy-p16-asus-npu2"
PARAM = Path("/sys/module/n1x_gpu_iova_observe/parameters/cpu_pages")
PAGE = 4096
SIZE = 9 * PAGE


def ram_coverage(iomem, address, size):
    names = []
    for line in iomem.splitlines():
        m = re.fullmatch(r"\s*([0-9a-f]+)-([0-9a-f]+) : (.+)", line)
        if m and int(m[1], 16) <= address and address + size <= int(m[2], 16) + 1:
            names.append(m[3])
    if not names or "System RAM" not in names or set(names) - {"System RAM", "reserved"}:
        raise ValueError(f"Candidate is not ordinary/reserved System RAM: {names}")


def capture(fd, pages, iomem):
    if len(pages) != 9 or len(set(pages)) != 9 or any(p <= 0 or p % PAGE for p in pages):
        raise ValueError("Expected nine distinct, aligned, nonzero CPU pages")
    hdr = os.pread(fd, 64, 0)
    if len(hdr) != 64 or hdr[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", hdr, 16) != (4, 183):
        raise ValueError("Expected AArch64 kernel core")
    phoff = struct.unpack_from("<Q", hdr, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", hdr, 54)
    if phsize != 56 or not 0 < phnum <= 4096:
        raise ValueError("Invalid core program headers")
    phdrs = os.pread(fd, phsize * phnum, phoff)
    if len(phdrs) != phsize * phnum:
        raise ValueError("Short program headers")
    segments = [(p, min(fs, ms), off) for typ, flags, off, va, p, fs, ms, align
                in struct.iter_unpack("<IIQQQQQQ", phdrs) if typ == 1 and fs]
    offsets = []
    for page in pages:
        ram_coverage(iomem, page, PAGE)
        hits = [off + page - pa for pa, size, off in segments
                if pa <= page and page + PAGE <= pa + size]
        if len(hits) != 1:
            raise ValueError("CPU page lacks a unique file-backed core segment")
        offsets.append(hits[0])
    header = os.pread(fd, 40, offsets[0])
    if len(header) != 40 or header[:4] != b"GFWI":
        raise ValueError("Translated RAM does not start with GFWI")
    start, length = struct.unpack_from("<II", header, 32)
    if not 40 <= start < SIZE or not 0 < length <= SIZE - start:
        raise ValueError("Unbounded ROM member")

    def sample():
        parts = [os.pread(fd, PAGE, offset) for offset in offsets]
        if any(len(part) != PAGE for part in parts):
            raise ValueError("Short page read")
        return b"".join(parts)

    blob = sample()
    if blob[:40] != header or sample() != blob:
        raise ValueError("Candidate contents changed during capture")
    return blob, start, length


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if os.uname().release != KERNEL:
        parser.error("Kernel differs from audited build")
    for name, value in (("sys_vendor", "ASUS"), ("product_name", "ProArt P16 H7607BA"),
                        ("bios_version", "H7607BA.300")):
        if Path("/sys/class/dmi/id", name).read_text().strip() != value:
            parser.error("Machine differs from audited board")
    raw = PARAM.read_text().strip()
    all_pages = [int(p, 0) for p in raw.split(",")]
    if len(all_pages) != 10:
        parser.error("Unexpected observer snapshot shape")
    fd = os.open("/proc/kcore", os.O_RDONLY | os.O_CLOEXEC)
    try:
        blob, start, length = capture(fd, all_pages[:9], Path("/proc/iomem").read_text())
    finally:
        os.close(fd)
    if PARAM.read_text().strip() != raw:
        parser.error("Observer snapshot changed")
    with args.output.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(blob)
        # Keep the capture readable by the authenticated local caller.
        if "PKEXEC_UID" in os.environ:
            os.fchown(output.fileno(), int(os.environ["PKEXEC_UID"]), -1)
    print(json.dumps(dict(kernel=KERNEL, boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                         candidate_iova="0xfc000000", cpu_pages=[hex(p) for p in all_pages],
                         bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
                         rom_offset=start, rom_bytes=length, stable_content_samples=2,
                         post_capture_translation_check_required=True), indent=2))


if __name__ == "__main__":
    main()
