#!/usr/bin/env python3
"""Execute original SROOT resource enumeration/mapping with synthetic WDF calls."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from test_sroot_send import ROOT, BASE, HASH

CTX, FRAME, LIST, DESC, TABLE, STUB, STACK = (0x22000000+i*0x10000 for i in range(7))


def run(pe, resources):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, length in ((0x30000, 0x3000), (0x19000, 0x1000), (0x1b000, 0x1000)):
        u.mem_map(BASE+rva, length)
        u.mem_write(BASE+rva, pe.get_data(rva, length))
    for address in (CTX, FRAME, LIST, DESC, TABLE, STUB, STACK):
        u.mem_map(address, 0x10000)
    ret = bytes.fromhex('c0035fd6')
    for offset in (0, 0x10, 0x20, 0x30):
        u.mem_write(STUB+offset, ret)
    u.mem_write(BASE+0x1b358, struct.pack('<Q', BASE+0x1b358))
    u.mem_write(BASE+0x1b838, struct.pack('<Q', TABLE))
    u.mem_write(BASE+0x19200, struct.pack('<Q', STUB))  # CFG check only.
    u.mem_write(BASE+0x191d0, struct.pack('<Q', STUB+0x30))
    u.mem_write(TABLE+0x980, struct.pack('<Q', STUB+0x10))
    u.mem_write(TABLE+0x988, struct.pack('<Q', STUB+0x20))
    u.mem_write(FRAME+8, struct.pack('<Q', CTX))
    for i, (kind, address, size) in enumerate(resources):
        u.mem_write(DESC+32*i, struct.pack('<BBHQI', kind, 0, 0, address, size))
    calls = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == STUB+0x10:
            assert reg(1) == LIST
            calls.append(('count', len(resources)))
            u.reg_write(regs.UC_ARM64_REG_X0, len(resources))
        elif address == STUB+0x20:
            assert reg(1) == LIST
            index = reg(2)
            calls.append(('descriptor', index))
            u.reg_write(regs.UC_ARM64_REG_X0, DESC+index*32)
        elif address == STUB+0x30:
            calls.append(('map', reg(0), reg(1), reg(2)))
            u.reg_write(regs.UC_ARM64_REG_X0, 0x23000000)

    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in ((20, BASE+0x1b358), (21, BASE+0x1b328), (26, FRAME), (27, LIST)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.emu_start(BASE+0x30704, BASE+0x309d0, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x309d0
    fields = {'first_base': struct.unpack('<Q', u.mem_read(CTX+0x38, 8))[0],
              'first_size': struct.unpack('<I', u.mem_read(CTX+0x40, 4))[0],
              'second_base': struct.unpack('<Q', u.mem_read(CTX+0x48, 8))[0],
              'stride': struct.unpack('<I', u.mem_read(CTX+0x50, 4))[0],
              'mapped_base': struct.unpack('<Q', u.mem_read(CTX+0x58, 8))[0]}
    return calls, fields


def power_callback(pe, entry):
    """Execute complete provider D0 callbacks with only KMDF/CFG stubs."""
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, length in ((0x30000, 0x1000), (0x19000, 0x1000), (0x1b000, 0x1000)):
        u.mem_map(BASE+rva, length)
        u.mem_write(BASE+rva, pe.get_data(rva, length))
    for address in (TABLE, STUB, STACK):
        u.mem_map(address, 0x10000)
    u.mem_write(BASE+0x1b358, struct.pack('<Q', BASE+0x1b358))
    u.mem_write(BASE+0x1b838, struct.pack('<Q', TABLE))
    u.mem_write(BASE+0x19200, struct.pack('<Q', STUB))
    names = {0x2e0: 'WdfDeviceGetDefaultQueue', 0x4d0: 'WdfIoQueueStart',
             0x4e0: 'WdfIoQueueStopSynchronously', 0x520: 'WdfIoQueuePurgeSynchronously'}
    device, queue = 0x45670000, 0x45680000
    targets, events = {}, []
    u.mem_write(STUB, bytes.fromhex('c0035fd6'))
    for index, (offset, name) in enumerate(names.items(), 1):
        address = STUB+16*index
        targets[address] = name
        u.mem_write(address, bytes.fromhex('c0035fd6'))
        u.mem_write(TABLE+offset, struct.pack('<Q', address))

    def hook(_, address, size, user):
        if address not in targets:
            return
        name = targets[address]
        argument = u.reg_read(regs.UC_ARM64_REG_X1)
        assert argument == (device if name == 'WdfDeviceGetDefaultQueue' else queue)
        events.append(name)
        u.reg_write(regs.UC_ARM64_REG_X0, queue if name == 'WdfDeviceGetDefaultQueue' else 0)

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, device)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STUB+0xff0)
    u.emu_start(BASE+entry, STUB+0xff0, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STUB+0xff0
    assert u.reg_read(regs.UC_ARM64_REG_X0) == 0
    return events


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path))
    imports = {item.address: item.name for entry in pe.DIRECTORY_ENTRY_IMPORT for item in entry.imports}
    assert imports[BASE+0x191d0] == b'MmMapIoSpaceEx'
    rows = []
    for base, size, stride in ((0x18010000, 0x1000, 0x10), (0x11000000, 0x2000, 4)):
        calls, fields = run(pe, [(3, base, size), (3, base, stride)])
        assert calls == [('count', 2), ('descriptor', 0), ('descriptor', 1), ('map', base, size, 0x204)]
        assert fields == {'first_base': base, 'first_size': size, 'second_base': base,
                          'stride': stride, 'mapped_base': 0x23000000}
        rows.append({'resources': [hex(base), hex(size), hex(stride)], 'calls': calls, 'fields': fields})
    calls, fields = run(pe, [(3, 0x18010000, 0x1000)])
    assert calls == [('count', 1)] and not any(fields.values())
    power = {hex(entry): power_callback(pe, entry) for entry in (0x302d0, 0x30380)}
    assert power['0x302d0'] == ['WdfDeviceGetDefaultQueue', 'WdfIoQueueStart']
    assert power['0x30380'] == ['WdfDeviceGetDefaultQueue', 'WdfIoQueueStopSynchronously',
                               'WdfDeviceGetDefaultQueue', 'WdfIoQueuePurgeSynchronously']
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'original_power_callbacks': power,
                      'wrong_resource_count_skips_mapping': True,
                      'conclusion': 'Second memory resource length becomes register stride; first resource is mapped',
                      'limitations': ['Synthetic translated WDF resources, not live Windows resource values',
                                      'Archived ROT0 resources imply stride 16 only if live tables match',
                                      'No ACPI evaluation, physical mapping, or register access']}, indent=2))


if __name__ == '__main__':
    main()
