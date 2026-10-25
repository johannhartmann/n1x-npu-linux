#!/usr/bin/env python3
"""Execute three Windows post-upload callbacks to their method/cache boundaries."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
CTX, BUFFER, OUTER, STACK, STUB = (0x2b000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


def run(pe, entry, cpu=BUFFER, dma=0x20001000, send_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva in (0xd46000, 0xd49000, 0xd38000, 0xd39000, 0xd3b000,
                0xce000, 0xd8000, 0xe3e000, 0x1512000, 0x13ac000):
        u.mem_map(BASE+rva, 0x1000)
        u.mem_write(BASE+rva, pe.get_data(rva, 0x1000))
    for address in (CTX, BUFFER, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160, 0xd38a38, 0xd3b298, 0xd38758, 0xd82c0, 0xce440, 0xce4b0):
        u.mem_write(BASE+rva, ret)
    for offset in (0, 16):
        u.mem_write(STUB+offset, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 3))
    u.mem_write(CTX+0xf8, struct.pack('<Q', STUB+16))
    u.mem_write(CTX+0x4f0, struct.pack('<QQQ', dma, cpu, dma))
    u.mem_write(BUFFER, b'\xa5'*4096)
    events = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, OUTER)
        elif address == BASE+0xd38a38:
            u.reg_write(regs.UC_ARM64_REG_X0, 0x12340000)
        elif address == BASE+0xd3b298:
            assert reg(0) == OUTER
            events.append({'cache_boundary': [reg(1), reg(2), reg(3)]})
        elif address == BASE+0xd82c0:
            events.append({'delay_argument': reg(0)})
        elif address == STUB+16:
            assert reg(0) == OUTER and reg(1) == CTX
            assert reg(4) == 0 and reg(5) == 0
            events.append({'command': reg(2), 'data': reg(3)})
            u.reg_write(regs.UC_ARM64_REG_X0, send_error)

    u.hook_add(UC_HOOK_CODE, hook)
    for register, value in ((regs.UC_ARM64_REG_X0, OUTER), (regs.UC_ARM64_REG_X1, CTX),
                            (regs.UC_ARM64_REG_SP, STACK+0x8000), (regs.UC_ARM64_REG_LR, STOP)):
        u.reg_write(register, value)
    u.emu_start(BASE+entry, STOP, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    assert bytes(u.mem_read(BUFFER, 4096)) == b'\xa5'*4096
    return {'entry': hex(entry), 'status': u.reg_read(regs.UC_ARM64_REG_X0), 'events': events}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for entry, command in ((0xd46300, 22), (0xd49570, 21), (0xd496e0, 23)):
        for kwargs in ({}, {'send_error': 0x65}, {'dma': 0}, {'dma': 255}):
            result = run(pe, entry, **kwargs)
            methods = [e for e in result['events'] if 'command' in e]
            if kwargs.get('dma', 0x20001000) < 256:
                assert result['status'] == (0x1f if entry == 0xd496e0 else 0x40) and methods == []
            else:
                assert result['status'] == kwargs.get('send_error', 0)
                assert methods == [{'command': command, 'data': 0x200010}]
                prefix = ([{'cache_boundary': [BUFFER, 4096, 1]}, {'delay_argument': 100}]
                          if entry == 0xd49570 else [])
                suffix = ([{'delay_argument': 100}, {'cache_boundary': [BUFFER, 4096, 0]}]
                          if entry != 0xd496e0 and not kwargs.get('send_error') else [])
                assert result['events'] == prefix+methods+suffix
            rows.append({'inputs': kwargs, **result})
    for entry in (0xd46300, 0xd49570):
        result = run(pe, entry, cpu=0)
        assert result['status'] == 0x40 and result['events'] == []
        rows.append({'inputs': {'cpu': 0}, **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'limitations': ['Original callback bodies execute; method/cache/delay/OS boundaries are stubs',
                                      'Cache helper arguments are recorded, not proof of physical cache effects or delay units',
                                      'Command data is encoded from the context DMA address, not inferred from CPU physical memory',
                                      'Payload contents and firmware processing are not validated by this test']}, indent=2))


if __name__ == '__main__':
    main()
