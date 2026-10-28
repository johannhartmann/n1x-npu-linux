#!/usr/bin/env python3
"""Execute original HAL-1 stop dispatch and power policy offline, not hardware."""
import hashlib
import json
import struct

import pefile
from unicorn import UC_HOOK_CODE

from inspect_kmd_reset_dispatch import DispatchMachine
from inspect_kmd_task_submission import BASE, PARENT, PRIMARY, OWNER, ROOT, KMD_SHA256


def run(pe, *, target=3, transport_error=0, firmware_error=0):
    m = DispatchMachine(pe)
    m.write(PARENT+0x14e, 'H', 3)
    m.call(0x194fb08, PARENT)
    m.write(PRIMARY+0x496, 'H', 1)
    m.call(0x1951238, PRIMARY)
    m.write(PRIMARY, 'QQ', BASE+0x108e9a0, BASE+0x108e9b8)
    m.write(PARENT+0x178, 'Q', PRIMARY+8)
    m.write(PARENT+0x318, 'I', target)
    m.write(PARENT+0x4b0, 'I', 0x777)
    hfrp, provider, link = (OWNER+i*0x1000 for i in range(3))
    for address, value in ((PARENT+0x300, hfrp), (hfrp+0x60, provider),
                           (provider+0x18, link)):
        m.write(address, 'Q', value)
    m.write(link+8, 'IBB', 1, 1, 5)
    m.write(BASE+0x13ac13c, 'I', 5)
    final = struct.unpack('<Q', pe.get_data(0x108e9a0+0x290, 8))[0]-BASE
    entries, packets, notifications = [], [], []

    def hook(_, pc, size, user):
        rva = pc-BASE
        if rva in (0xd39de0, 0x1423b0, 0xd447d0, 0xd4a4f0, 0xd405f0,
                   0x1423e0, 0xd44870):
            entries.append(hex(rva))
        if rva in (0xd3de30, 0xd3dd40):
            assert m.reg(0) == hfrp and m.reg(1) == 400 and m.reg(3) == 2
            packets.append(bytes(m.u.mem_read(m.reg(2), 2)).hex())
            m.write(m.reg(4), 'H', firmware_error)
            m.write(m.reg(5), 'H', 0x43)
            m.write(m.reg(6), 'I', 2)
            if rva == 0xd3dd40:
                from unicorn import arm64_const as regs
                m.write(m.reg(7), 'Q', 1)
                pointer = m.read(m.u.reg_read(regs.UC_ARM64_REG_SP), 'Q')[0]
                m.write(pointer, 'Q', 2)
            m.ret(transport_error)
        elif rva == 0xd39ef0:
            notifications.append([m.reg(2), m.reg(3)])
            m.ret(0)
        elif rva == final:
            entries.append('allocation_teardown_boundary:'+hex(final))
            m.ret(0)
        elif rva in (0xd38a38, 0xd38758, 0xd38970, 0xd3f458,
                     0xd3f4f0, 0xce440, 0xce4b0):
            m.ret(0)

    m.u.hook_add(UC_HOOK_CODE, hook)
    code = m.call(0xd39de0, PARENT)
    return dict(status=code, target=target, entries=entries, packets=packets,
                notifications=notifications, pending=m.read(PARENT+0x4a8, 'I')[0])


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for options in ({}, {'target': 1}, {'transport_error': 0x65}, {'firmware_error': 1}):
        row = run(pe, **options)
        assert row['status'] == 0, row  # Original HAL ignores power failure.
        assert row['packets'] == (['4300', '4300'] if options.get('transport_error') or
                                  options.get('firmware_error') else ['4300']), row
        assert '0xd447d0' in row['entries'] and '0xd4a4f0' in row['entries'], row
        teardown = any(e.startswith('allocation_teardown_boundary:') for e in row['entries'])
        assert teardown == (options.get('target', 3) == 3), row
        rows.append(dict(inputs=options, **row))
    print(json.dumps(dict(passed=True, hardware_access=False, kmd_sha256=KMD_SHA256,
                         cases=rows, limitations=[
                             'HFRP transport, OS notifications/timing and allocation teardown are modeled',
                             'Original stop dispatch and power encoder execute unchanged',
                             'Windows return zero is NOT a DMA-quiescence proof; Linux must validate gating']), indent=2))


if __name__ == '__main__':
    main()
