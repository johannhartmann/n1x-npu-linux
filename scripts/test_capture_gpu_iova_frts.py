#!/usr/bin/env python3
"""Check noncontiguous RAM acquisition and rejection before bulk capture."""
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from capture_gpu_iova_frts import capture, PAGE, SIZE


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.TemporaryFile()
        self.pages = [0x100000 + i * 2 * PAGE for i in range(9)]
        self.iomem = "00100000-001fffff : System RAM\n  00100000-001fffff : reserved"
        self.blob = bytearray((n * 17) % 256 for n in range(SIZE))
        self.blob[:4] = b"GFWI"
        struct.pack_into("<II", self.blob, 32, 2048, SIZE - 2048)
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 4, 183)
        struct.pack_into("<Q", header, 32, 64)
        struct.pack_into("<HH", header, 54, 56, 9)
        self.file.write(header)
        for i, page in enumerate(self.pages):
            self.file.write(struct.pack("<IIQQQQQQ", 1, 4, (i+1)*PAGE,
                                        0xffff000000000000+page, page, PAGE, PAGE, PAGE))
        for i in range(9):
            self.file.seek((i+1)*PAGE)
            self.file.write(self.blob[i*PAGE:(i+1)*PAGE])
        self.file.flush()

    def tearDown(self):
        self.file.close()

    def test_noncontiguous_pages(self):
        data, offset, length = capture(self.file.fileno(), self.pages, self.iomem)
        self.assertEqual((data, offset, length), (self.blob, 2048, SIZE-2048))

    def test_duplicate_or_unmapped_page(self):
        for pages in ([self.pages[0]]*9, [0]+self.pages[1:], [p+1 for p in self.pages],
                      self.pages[:-1]+[0x300000]):
            with self.assertRaises(ValueError):
                capture(self.file.fileno(), pages, self.iomem)

    def test_device_region_rejected(self):
        with self.assertRaises(ValueError):
            capture(self.file.fileno(), self.pages, self.iomem.replace("reserved", "Device MMIO"))

    def test_invalid_header_and_extent(self):
        for offset, data in ((0, b"NOPE"), (32, struct.pack("<II", SIZE-1, 2))):
            os.pwrite(self.file.fileno(), data, PAGE+offset)
            with self.assertRaises(ValueError):
                capture(self.file.fileno(), self.pages, self.iomem)
            os.pwrite(self.file.fileno(), self.blob[offset:offset+len(data)], PAGE+offset)

    def test_unstable_contents(self):
        original = os.pread
        count = 0
        def read(fd, size, offset):
            nonlocal count
            data = original(fd, size, offset)
            if size == PAGE:
                count += 1
                if count == 10:
                    data = bytes([data[0] ^ 1])+data[1:]
            return data
        with patch("os.pread", read), self.assertRaises(ValueError):
            capture(self.file.fileno(), self.pages, self.iomem)


if __name__ == "__main__":
    unittest.main()
