#!/usr/bin/env python3
"""Check the cached-metadata reader's file bounds and pinned instruction guards."""
import io
import struct
import tempfile
import unittest

from elftools.elf.elffile import ELFFile
from read_gsp_frts_cache import CoreReader, CHECKS, MODULE, inspect_cache, pointer

BASE = 0xFFFF000001000000


def core_file(*, overlapping=False, truncate=False):
    f = tempfile.TemporaryFile()
    hdr = bytearray(64)
    hdr[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", hdr, 16, 4, 183)
    struct.pack_into("<Q", hdr, 32, 64)
    struct.pack_into("<HH", hdr, 54, 56, 2 if overlapping else 1)
    ph = struct.pack("<IIQQQQQQ", 1, 4, 4096, BASE, 0, 32, 32, 4096)
    f.write(hdr + ph + (ph if overlapping else b""))
    f.seek(4096)
    f.write(bytes(range(16 if truncate else 32)))
    f.flush()
    return f


class Tests(unittest.TestCase):
    def test_file_backed_bounds(self):
        with core_file() as f:
            r = CoreReader(f.fileno())
            self.assertEqual(r.read(BASE + 24, 8), bytes(range(24, 32)))
            for addr, size in ((BASE - 1, 8), (BASE + 31, 2), (BASE, 257),
                               (BASE, 0), ((1 << 64) - 4, 8)):
                with self.assertRaises(ValueError):
                    r.read(addr, size)

    def test_ambiguous_segment(self):
        with core_file(overlapping=True) as f:
            with self.assertRaisesRegex(ValueError, "unique"):
                CoreReader(f.fileno()).read(BASE, 8)

    def test_short_read(self):
        with core_file(truncate=True) as f:
            with self.assertRaisesRegex(ValueError, "Short"):
                CoreReader(f.fileno()).read(BASE + 16, 8)

    def test_bad_header(self):
        with tempfile.TemporaryFile() as f:
            f.write(b"not a core")
            f.flush()
            with self.assertRaises(ValueError):
                CoreReader(f.fileno())

    def test_pointer_rejection(self):
        self.assertEqual(pointer(BASE), BASE)
        for value in (0, 0x80000000, BASE + 1):
            with self.assertRaises(ValueError):
                pointer(value)

    def test_instruction_guards_match_module(self):
        elf = ELFFile(io.BytesIO(MODULE.read_bytes()))
        symtab = elf.get_section_by_name(".symtab")
        for name, checks in CHECKS.items():
            symbols = symtab.get_symbol_by_name(name)
            self.assertEqual(len(symbols), 1)
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            data = section.data()
            start = symbol["st_value"]
            for off, word in checks.items():
                self.assertLessEqual(off + 4, symbol["st_size"])
                self.assertEqual(data[start + off:start + off + 4], struct.pack("<I", word),
                                 f"{name}+{off:#x}")

    def test_changed_code_stops_before_cache(self):
        class BadCode:
            def read(self, address, size):
                return b"\xff" * size

            def integer(self, *args):
                raise AssertionError("Cache read before code validation")

        with self.assertRaisesRegex(ValueError, "Running instruction differs"):
            inspect_cache(BadCode(), {name: BASE for name in CHECKS})

    def test_cache_consistency(self):
        # A bounded synthetic snapshot with different addresses for every
        # object. Unknown reads fail, including unintended GPU/MMIO reads.
        memory = {}

        def put(address, value, size=8):
            for i, byte in enumerate(value.to_bytes(size, "little")):
                memory[address + i] = byte

        class Snapshot:
            def read(self, address, size):
                return bytes(memory[address + i] for i in range(size))

            def integer(self, address, size=8):
                return int.from_bytes(self.read(address, size), "little")

        symbols = {name: BASE + i * 0x1000 for i, name in enumerate(CHECKS)}
        for name, checks in CHECKS.items():
            for offset, word in checks.items():
                put(symbols[name] + offset, word, 4)
        symbols["g_pSys"] = BASE + 0x3000
        system, manager, gpu, kgsp, wpr = (BASE + x for x in
                                         (0x10000, 0x20000, 0x70000, 0x80000, 0x100000))
        fields = ((symbols["g_pSys"], system, 8), (system + 0x1e8, manager, 8),
                  (manager + 0x48960, 1, 4), (manager + 0x48760, gpu, 8),
                  (manager + 0x48768, 0, 4), (gpu + 0x4a5, 1, 1),
                  (gpu + 0x992, 0, 1), (gpu + 0x4d0, 1, 1),
                  (gpu + 0x1c30, kgsp, 8), (kgsp + 0x6a660, 0x1234000, 8),
                  (kgsp + 0xb08, 0x1234000, 8), (kgsp + 0xb10, 0x100000, 8),
                  (kgsp + 0xae0, wpr, 8), (wpr + 0xa0, 0x100000, 8))
        for args in fields:
            put(*args)
        result = inspect_cache(Snapshot(), symbols)
        self.assertEqual(result["frts_offset"], "0x1234000")
        self.assertEqual(result["host_requested_frts_size"], 0x100000)
        # The actual GB20B host HAL requests zero, although the firmware can
        # return a nonzero offset. Preserve that observation without inventing
        # a readable container extent.
        put(kgsp + 0xb10, 0)
        put(wpr + 0xa0, 0)
        self.assertEqual(inspect_cache(Snapshot(), symbols)["host_requested_frts_size"], 0)
        put(kgsp + 0xb10, 0x100000)
        put(wpr + 0xa0, 0x100000)
        for address, size in ((kgsp + 0xb08, 8), (wpr + 0xa0, 8),
                              (gpu + 0x4a5, 1), (gpu + 0x4d0, 1),
                              (manager + 0x48960, 4)):
            original = Snapshot().integer(address, size)
            put(address, 0, size)
            with self.assertRaises(ValueError):
                inspect_cache(Snapshot(), symbols)
            put(address, original, size)


if __name__ == "__main__":
    unittest.main()
