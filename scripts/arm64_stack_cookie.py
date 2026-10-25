#!/usr/bin/env python3
"""Preserve original KMD stack-cookie helpers and their 16-byte stack frame."""
import struct

BASE = 0x140000000


def install(uc, pe):
    # Caller maps the helper's existing code page. Only the process-global
    # cookie storage is new synthetic RAM; no kernel or device is accessed.
    uc.mem_map(BASE+0x11ff000, 0x1000)
    uc.mem_write(BASE+0x11ff5c0, struct.pack('<Q', 0x12345678))
    uc.mem_write(BASE+0xd57b60, pe.get_data(0xd57b60, 0x50))
