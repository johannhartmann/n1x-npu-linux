#!/usr/bin/env python3
"""Join the proven subobject vtable, forwarding thunks and emulated accessors."""
import hashlib
import json
import struct
from pathlib import Path
import pefile
from extract_firmware import KMD_SHA256
from emulate_register_accessors import decode

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
VTABLE_RVA = 0x108e9b8
SLOTS = (0x40, 0x48, 0x50, 0x60, 0x70, 0x78, 0x80, 0x90,
         0xf8, 0x100, 0x108, 0x118, 0x128, 0x138, 0x148, 0x150, 0x158, 0x160, 0x168, 0x1d0)
# Register offsets match L4T R38.4 where defined. Block-index roles
# are observed in Windows; 0x210 is absent from that public header.
EXPECTED = {
    0x40: ('write', 0x180, 'IMEMC'), 0x48: ('read', 0x184, 'IMEMD'),
    0x50: ('write', 0x184, 'IMEMD'), 0x60: ('write', 0x188, 'IMEM_BLOCK_INDEX'),
    0x70: ('write', 0x1c0, 'DMEMC'), 0x78: ('read', 0x1c4, 'DMEMD'),
    0x80: ('write', 0x1c4, 'DMEMD'), 0x90: ('write', 0x210, 'DMEM_BLOCK_INDEX'),
    0xf8: ('write', 0x48, 'ITFEN'),
    0x100: ('read', 0x444, 'TRANSCFG'), 0x108: ('write', 0x444, 'TRANSCFG'),
    0x118: ('write', 0x10c, 'DMACTL'), 0x128: ('write', 0x110, 'DMATRFBASE'),
    0x138: ('write', 0x114, 'DMATRFMOFFS'),
    0x148: ('write', 0x11c, 'DMATRFFBOFFS'),
    0x150: ('read', 0x118, 'DMATRFCMD'), 0x158: ('write', 0x118, 'DMATRFCMD'),
    0x160: ('read', 0xb88, 'CPUCTL'), 0x168: ('write', 0xb88, 'CPUCTL'),
    0x1d0: ('read', 0x4c, 'IDLESTATE')}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    accessors = {int(r['hal_slot'], 16): r for r in decode(pe) if r['index'] == 0}
    rows = []
    # Constructor RVA 0x195d114..134 installs descriptor+0x18 at object+8.
    assert pe.get_data(0x195d114, 0x24).hex() == (
        '88b9ffb00981269128c10091a90200f9e103182a880200f9'
        '28610091a05a1291680200f9')
    for slot in SLOTS:
        thunk = struct.unpack('<Q', pe.get_data(VTABLE_RVA+slot, 8))[0]
        code = pe.get_data(thunk-BASE, 0x30)
        assert code[:16].hex() == 'fd7bbfa9fd030091e80301aa012100d1'
        ins = struct.unpack_from('<I', code, 16)[0]
        assert ins & 0xffc003ff == 0xf9400108  # LDR X8,[X8,#imm]
        hal_slot = ((ins>>10)&4095)*8+8  # Subobject is parent+8.
        entry = accessors[hal_slot]
        operation, offset, name = EXPECTED[slot]
        assert entry['operation'] == operation and int(entry['offset'], 16) == offset
        rows.append({'subobject_slot': hex(slot), 'forwarder_rva': hex(thunk-BASE),
                     'hal_slot': hex(hal_slot), 'accessor_rva': entry['accessor_rva'],
                     'operation': operation, 'offset': hex(offset), 'register': name})
    print(json.dumps({'scope': 'Static constructor/thunk checks plus original RAM-only HAL emulation',
                      'subobject_offset': 8, 'vtable_rva': hex(VTABLE_RVA),
                      'resource_index': 0, 'mapping_to_live_physical_resource': 'pending',
                      'transfer_registers': rows}, indent=2))


if __name__ == '__main__':
    main()
