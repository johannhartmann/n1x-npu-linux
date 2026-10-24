#!/usr/bin/env python3
"""Run the actual firmware address-list cache loader and lookup with RAM DMA."""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV64, UC_HOOK_CODE
from unicorn import riscv_const as regs
from emulate_firmware_commands import ROOT, BLOB_HASH

CTX, TASK, LIST, OUTPUT, STACK, STOP = 0x20000000, 0x20001000, 0x20002000, 0x20004000, 0x20100000, 0x20200000


def run(blob, addresses, indices, dma_error=False, raw_list=None):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64)
    for address, size in ((0x100000, 0xb000), (0x180000, 0x10000),
                          (CTX, 0x10000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[0x200:0xb100])
    u.mem_write(0x180000, blob[0xb100:])
    for address in (0x101e9a, 0x1054da, 0x10559e):
        u.mem_write(address, bytes.fromhex('67800000'))
    u.mem_write(CTX, struct.pack('<Q', TASK))
    u.mem_write(TASK, struct.pack('<QH', LIST, len(addresses)))
    wire = b''.join(struct.pack('<Q', address) for address in addresses) if raw_list is None else raw_list
    if wire:
        u.mem_write(LIST, bytes(wire))
    transfers = []

    def hook(_, address, size, user):
        if address == 0x101e9a:
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address in (0x1054da, 0x10559e):
            source, destination, length = (u.reg_read(r) for r in
                (regs.UC_RISCV_REG_A0, regs.UC_RISCV_REG_A1, regs.UC_RISCV_REG_A2))
            assert LIST <= source and source+length <= OUTPUT
            transfers.append({'source': hex(source), 'destination': hex(destination), 'bytes': length})
            if length and not dma_error:
                u.mem_write(destination, bytes(u.mem_read(source, length)))
            u.reg_write(regs.UC_RISCV_REG_A0, (1 << 64)-4 if dma_error else 0)

    def call(entry, *args):
        for index, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_RISCV_REG_A{index}'), value & ((1 << 64)-1))
        u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(entry, STOP, count=10000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        value = u.reg_read(regs.UC_RISCV_REG_A0)
        return value-(1 << 64) if value >= 1 << 63 else value

    u.hook_add(UC_HOOK_CODE, hook)
    status = call(0x104ed6, CTX)
    lookups = []
    if status == 0:
        for index in indices:
            u.mem_write(OUTPUT, bytes.fromhex('efbeaddeefbeadde'))
            code = call(0x104fc0, CTX, index, OUTPUT)
            value = struct.unpack('<Q', u.mem_read(OUTPUT, 8))[0]
            lookups.append({'index': index, 'status': code, 'value': value})
    return {'status': status, 'transfers': transfers, 'lookups': lookups}


def main():
    blob = (ROOT/'sources/dla-t35x-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == BLOB_HASH
    rows = []
    for count in (1, 9, 31, 32, 33, 511, 512):
        addresses = [0x123456780000+i*256 for i in range(count)]
        result = run(blob, addresses, (0, count-1, count, count+1, -1))
        assert result['status'] == 0
        assert [x['bytes'] for x in result['transfers']] == [(count*8+255)&~255, 8, 8, 8]
        for lookup in result['lookups']:
            index = lookup['index']
            if 0 <= index < count:
                assert lookup['status'] == 0 and lookup['value'] == addresses[index]
            elif index == count:
                assert lookup['status'] == 0 and lookup['value'] == 0
            else:
                assert lookup['status'] == -3 and lookup['value'] == 0xdeadbeefdeadbeef
        rows.append({'case': f'cached_{count}_addresses', **result})
    result = run(blob, [0x12340000], (), dma_error=True)
    assert result['status'] == -4 and not result['lookups']
    rows.append({'case': 'cache_dma_failure', **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'firmware_sha256': BLOB_HASH,
                      'loader': '0x104ed6', 'lookup': '0x104fc0', 'cases': rows,
                      'limitations': ['Uses a synthetic firmware task context, not queue submission',
                                      'Only the cached path up to 512 addresses is exercised',
                                      'Physical DMA, model bindings and inference remain untested']}, indent=2))


if __name__ == '__main__':
    main()
