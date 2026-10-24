#!/usr/bin/env python3
"""Execute P16 RV32 address-list loader/lookup; DMA and logging remain RAM stubs."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE
from unicorn import riscv_const as regs
from extract_profile1_firmware import PROFILE1_HASH
from emulate_address_list import ROOT, CTX, TASK, LIST, OUTPUT, STACK, STOP


def run(blob, addresses, indices, dma_error=False, raw_list=None):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (CTX, 0x10000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    for address in (0x101c96, 0x104ed8):
        u.mem_write(address, bytes.fromhex('67800000'))
    u.mem_write(CTX, struct.pack('<I', TASK))
    u.mem_write(TASK, struct.pack('<QH', LIST, len(addresses)))
    wire = b''.join(struct.pack('<Q', address) for address in addresses) if raw_list is None else raw_list
    if wire:
        u.mem_write(LIST, bytes(wire))
    transfers = []

    def hook(_, pc, size, user):
        if pc == 0x101c96:
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif pc == 0x104ed8:
            a = [u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}')) for i in range(5)]
            source, destination, length = a[0] | a[1] << 32, a[2], a[3]
            assert LIST <= source and source+length <= OUTPUT
            transfers.append({'source': hex(source), 'destination': hex(destination), 'bytes': length})
            if length and not dma_error:
                u.mem_write(destination, bytes(u.mem_read(source, length)))
            u.reg_write(regs.UC_RISCV_REG_A0, 0xfffffffc if dma_error else 0)

    def call(entry, *args):
        for index, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_RISCV_REG_A{index}'), value & 0xffffffff)
        u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(entry, STOP, count=10000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        value = u.reg_read(regs.UC_RISCV_REG_A0)
        return value-(1 << 32) if value >= 1 << 31 else value

    u.hook_add(UC_HOOK_CODE, hook)
    status = call(0x104a92, CTX)
    lookups = []
    if status == 0:
        for index in indices:
            u.mem_write(OUTPUT, bytes.fromhex('efbeaddeefbeadde'))
            code = call(0x104b62, CTX, index, OUTPUT)
            value = struct.unpack('<Q', u.mem_read(OUTPUT, 8))[0]
            lookups.append({'index': index, 'status': code, 'value': value})
    return {'status': status, 'transfers': transfers, 'lookups': lookups}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    rows = []
    for count in (1, 9, 31, 32, 33, 511, 512):
        addresses = [0x123456780000+i*256 for i in range(count)]
        result = run(blob, addresses, (0, count-1, count, count+1, -1, 0x10000))
        assert result['status'] == 0
        # HAL-1 passes exact count*8 to its DMA helper, unlike T35x's rounded request.
        assert [x['bytes'] for x in result['transfers']] == [count*8]
        for item in result['lookups']:
            index = item['index']
            if 0 <= index < count:
                assert item['status'] == 0 and item['value'] == addresses[index]
            elif index == count:
                assert item['status'] == 0  # Original off-by-one; never rely on its value.
            elif index == 0x10000:
                assert item['status'] == 0 and item['value'] == addresses[0]
            else:
                assert item['status'] == -3 and item['value'] == 0xdeadbeefdeadbeef
        rows.append({'case': f'cached_{count}_addresses', **result})
    result = run(blob, [0x12340000], (), dma_error=True)
    assert result['status'] == -4 and not result['lookups']
    rows.append({'case': 'cache_dma_failure', **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'architecture': 'RV32',
                      'firmware_sha256': PROFILE1_HASH, 'loader': '0x104a92', 'lookup': '0x104b62',
                      'cases': rows, 'limitations': ['Synthetic firmware context and stubbed DMA',
                      'Cached path only (up to 512 entries); no paged lookup execution',
                      'Lookup accepts index==count and truncates most indices to 16 bits',
                      'DMA helper call lengths alone do not prove physical transfer extents']}, indent=2))


if __name__ == '__main__':
    main()
