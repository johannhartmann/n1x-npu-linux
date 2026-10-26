#!/usr/bin/env python3
"""Synthetic protobuf boundary tests; no driver or hardware access."""
import unittest
from decode_vbios_scratch import decode, fields, varint


def vint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def field(number, value):
    if isinstance(value, bytes):
        return vint(number << 3 | 2) + vint(len(value)) + value
    return vint(number << 3) + vint(value)


def capture(words, base=0x1400, extra=b""):
    registers = field(1, 1) + field(2, base) + field(4, b"".join(map(vint, words)))
    return field(3, field(111, field(12, registers) + extra))


class ScratchTests(unittest.TestCase):
    def test_observed_layout(self):
        words = [0] * 61 + [0xfc000000, 0, 0x10009]
        result = decode(capture(words))
        self.assertFalse(result["raw_rom_field_present"])
        self.assertEqual(result["candidate_frts"]["address"], "0xfc000000")
        self.assertEqual(result["candidate_frts"]["size"], 0x9000)
        self.assertEqual(result["candidate_frts"]["media"], "sysmem")
        self.assertFalse(result["candidate_frts"]["cpu_physical_mapping_verified"])

    def test_unknown_bits_and_high_address_preserved(self):
        result = decode(capture([0] * 61 + [0, 3, 0x80000009], extra=field(10, b"rom")))
        self.assertTrue(result["raw_rom_field_present"])
        self.assertEqual(result["candidate_frts"]["address"], "0x300000000")
        self.assertEqual(result["candidate_frts"]["unknown_config_bits"], "0x80000000")

    def test_invalid_register_shapes(self):
        for blob in (capture([0] * 63), capture([0] * 65), capture([1 << 32] * 64),
                     capture([0] * 64, base=0x1500), capture([0] * 64) * 2):
            with self.subTest(blob=blob[:10]), self.assertRaises(ValueError):
                decode(blob)

    def test_truncation_and_overflow(self):
        blob = capture([0] * 61 + [0xfc000000, 0, 0x10009])
        for size in range(len(blob)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode(blob[:size])
        for blob in (b"\x80" * 10, b"\xff" * 9 + b"\x02"):
            with self.assertRaises(ValueError):
                varint(blob, 0)
        for blob in (b"\x00\x00", b"\x0b", b"\x0a\x10a"):
            with self.assertRaises(ValueError):
                fields(blob)


if __name__ == "__main__":
    unittest.main()
