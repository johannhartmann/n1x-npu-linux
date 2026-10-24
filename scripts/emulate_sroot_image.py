#!/usr/bin/env python3
"""Trace original image validation, copy, request packing and DMA cleanup offline."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from test_sroot_send import ROOT, BASE, HASH

CTX, REQUEST, MEMORY, INPUT, BUFFER, ADAPTER, OPS, TABLE, STUB, STACK = (
    0x25000000+i*0x10000 for i in range(10))
STOP = STUB+0xff0


def machine(pe):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    image = pe.get_memory_mapped_image()
    u.mem_map(BASE, (len(image)+4095) & ~4095)
    u.mem_write(BASE, image)
    for address in (CTX, REQUEST, MEMORY, INPUT, BUFFER, ADAPTER, OPS, TABLE, STUB, STACK):
        u.mem_map(address, 0x10000)
    for offset in (0, 0x10, 0x20, 0x30):
        u.mem_write(STUB+offset, bytes.fromhex('c0035fd6'))
    for address, value in ((BASE+0x1b358, BASE+0x1b358), (BASE+0x1b838, TABLE),
                           (BASE+0x19200, STUB), (TABLE+0x858, STUB+0x10),
                           (TABLE+0x610, STUB+0x20), (ADAPTER+8, OPS), (OPS+0x18, STUB+0x30)):
        u.mem_write(address, struct.pack('<Q', value))
    return u


def call(u, rva, args, end=STOP):
    for i, value in enumerate(args):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+rva, end, count=100000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == end
    return u.reg_read(regs.UC_ARM64_REG_X0) & 0xffffffff


def validate_copy(pe, tag, size=64, maximum=0x10000):
    u, calls = machine(pe), []
    blob = struct.pack('<I', tag)+bytes(i & 255 for i in range(max(size-4, 0)))
    u.mem_write(INPUT, blob)
    u.mem_write(CTX+0x30, struct.pack('<I', maximum))

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, length, user):
        if address == STUB+0x10:
            assert reg(1) == REQUEST
            u.mem_write(reg(2), struct.pack('<Q', MEMORY))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x20:
            assert reg(1) == MEMORY
            u.mem_write(reg(2), struct.pack('<Q', size))
            u.reg_write(regs.UC_ARM64_REG_X0, INPUT)
        elif address == BASE+0x18030:
            calls.append((reg(0), reg(1), reg(2)))
            u.mem_write(reg(0), bytes(u.mem_read(reg(1), reg(2))))
            u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    status = call(u, 0x314a0, (REQUEST, CTX))
    if status:
        assert bytes(u.mem_read(CTX+0x68, 12)) == bytes(12)
        assert struct.unpack('<I', u.mem_read(CTX+0x1c, 4))[0] == 0
        return status, calls, None
    assert struct.unpack('<I', u.mem_read(CTX+0x1c, 4))[0] == tag
    assert struct.unpack('<I', u.mem_read(CTX+0x70, 4))[0] == size
    u.mem_write(CTX+0x78, struct.pack('<Q', BUFFER))
    u.mem_write(BUFFER, b'\xa5'*size)
    assert call(u, 0x39b0, (CTX,)) == 0
    assert calls == [(BUFFER, INPUT+4, size-4)]
    result = bytes(u.mem_read(BUFFER, size))
    assert result == blob[4:]+b'\xa5'*4
    return status, calls, {'tag': hex(tag), 'input_bytes': size, 'copied_bytes': size-4,
                           'untouched_tail_hex': result[-4:].hex()}


def request(pe):
    u = machine(pe)
    frame = STACK+0x4000
    u.mem_write(CTX+0x1c, struct.pack('<I', 0xc0302))
    u.mem_write(CTX+0x70, struct.pack('<I', 64))
    u.mem_write(CTX+0x80, struct.pack('<Q', 0x40100000))
    for i, value in ((19, CTX), (20, BASE+0x1b358), (26, frame)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.emu_start(BASE+0x31f18, BASE+0x31fb4, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x31fb4
    argument = u.reg_read(regs.UC_ARM64_REG_X0)
    raw = bytes(u.mem_read(argument, 72))
    assert raw[8] == 0x18 and raw[0x18] == 3
    pointer = struct.unpack_from('<Q', raw, 0x10)[0]
    words = list(struct.unpack('<III', u.mem_read(pointer, 12)))
    assert words == [0xc0302, 64, 0x40100000]
    return {'command': '0x18', 'payload': words}


def cleanup(pe, logical=0x40100000):
    u, calls = machine(pe), []
    for offset, value in ((0x60, ADAPTER), (0x68, INPUT), (0x78, BUFFER), (0x80, logical),
                          (0x88, 0x12345678000)):
        u.mem_write(CTX+offset, struct.pack('<Q', value))
    u.mem_write(CTX+0x70, struct.pack('<I', 64))

    def hook(_, address, length, user):
        if address == STUB+0x30:
            args = [u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(5)]
            assert args == [ADAPTER, 64, logical, BUFFER, 0]
            calls.append(args)

    u.hook_add(UC_HOOK_CODE, hook)
    call(u, 0x3e00, (CTX,))
    if logical:
        assert len(calls) == 1
        assert bytes(u.mem_read(CTX+0x68, 0x28)) == bytes(0x28)
        assert struct.unpack('<Q', u.mem_read(CTX+0x60, 8))[0] == ADAPTER
    else:
        assert not calls and struct.unpack('<Q', u.mem_read(CTX+0x78, 8))[0] == BUFFER
    return {'logical': hex(logical), 'free_calls': calls}


def cleanup_after_transport(pe, transport_status, reply_status):
    u, calls = machine(pe), []
    frame = STACK+0x4000
    for offset, value in ((8, 0x1234), (0x10, 0x5678), (0x60, ADAPTER),
                          (0x68, INPUT), (0x78, BUFFER), (0x80, 0x40100000)):
        u.mem_write(CTX+offset, struct.pack('<Q', value))
    u.mem_write(CTX+0x70, struct.pack('<I', 64))
    u.mem_write(frame, b'\x01')
    u.mem_write(frame+8, struct.pack('<I', reply_status))
    u.mem_write(TABLE+0xa00, struct.pack('<Q', STUB+0x10))
    u.mem_write(TABLE+0x9d0, struct.pack('<Q', STUB+0x20))

    def hook(_, address, length, user):
        if address == STUB+0x30:
            calls.append('FreeCommonBuffer')
        elif address == STUB+0x10:
            calls.append('WdfTimerStop')
        elif address == STUB+0x20:
            calls.append('WdfWaitLockRelease')

    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in ((0, transport_status), (19, CTX), (20, BASE+0x1b358),
                     (23, BASE+0x1b328), (24, 0), (26, frame)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.emu_start(BASE+0x31fb8, BASE+0x32194, count=100000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x32194
    result = u.reg_read(regs.UC_ARM64_REG_X24) & 0xffffffff
    assert result == (0xc0000229 if transport_status or reply_status else 0)
    assert calls == ['FreeCommonBuffer', 'WdfTimerStop', 'WdfWaitLockRelease']
    return {'transport_status': transport_status, 'reply_status': reply_status,
            'result': hex(result), 'calls': calls}


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path), fast_load=True)
    accepted = (0xc0300, 0xc0302, 0xc0303, 0xc0304, 0xc0305, 0x90400)
    rejected = (0, 0xc0301, 0xc0306, 0xffffffff)
    rows = []
    for tag in accepted:
        status, _, row = validate_copy(pe, tag)
        assert status == 0
        rows.append(row)
    for tag in rejected:
        assert validate_copy(pe, tag)[0] == 0xc0000206
    assert validate_copy(pe, 0xc0302, maximum=63)[0] == 0xc0000206
    for size in (32, 65, 512):
        assert validate_copy(pe, 0xc0302, size)[0] == 0
    packed = request(pe)
    freed = [cleanup(pe), cleanup(pe, 0)]
    suffixes = [cleanup_after_transport(pe, a, b) for a, b in ((0, 0), (1, 0), (0, 1), (0, 0x80000))]
    print(json.dumps({'passed': True, 'hardware_access': False, 'accepted_tags': rows,
                      'rejected_tags': [hex(t) for t in rejected], 'request': packed,
                      'cleanup': freed, 'upload_suffix_cases': suffixes,
                      'limitations': ['WDF callbacks, memcpy and FreeCommonBuffer are synthetic stubs',
                                      'Four tail bytes are not initialized by the observed copy routine',
                                      'Cleanup arguments do not prove device DMA completion or coherency']}, indent=2))


if __name__ == '__main__':
    main()
