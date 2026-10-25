#!/usr/bin/env python3
"""Recover original common-buffer DMA allocation contract using synthetic OS calls."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from test_sroot_send import ROOT, BASE, HASH

CTX, REQUEST, ADAPTER, OPS, TABLE, STUB, BUFFER, STACK = (0x24000000+i*0x10000 for i in range(8))


def run(pe, logical=0x40010000, lower=0x40000000, upper=0x4fffffff, allocated=True, device=REQUEST):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, length in ((0x3000, 0x1000), (0x19000, 0x1000), (0x1b000, 0x1000)):
        u.mem_map(BASE+rva, length)
        u.mem_write(BASE+rva, pe.get_data(rva, length))
    for address in (CTX, REQUEST, ADAPTER, OPS, TABLE, STUB, BUFFER, STACK):
        u.mem_map(address, 0x10000)
    for offset in (0, 0x10, 0x20, 0x30, 0x40):
        u.mem_write(STUB+offset, bytes.fromhex('c0035fd6'))
    u.mem_write(BASE+0x1b358, struct.pack('<Q', BASE+0x1b358))
    for address, value in ((BASE+0x1b838, TABLE), (BASE+0x19200, STUB),
                           (TABLE+0x108, STUB+0x10), (BASE+0x191a8, STUB+0x20),
                           (BASE+0x191a0, STUB+0x40), (ADAPTER+8, OPS), (OPS+0x110, STUB+0x30)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(CTX+0x28, struct.pack('<II', lower, upper))
    u.mem_write(CTX+0x70, struct.pack('<I', 0x1234))
    # Poison stack so cache-policy evidence cannot come from zero-filled RAM.
    u.mem_write(STACK, b'\xa5'*0x10000)
    calls = []

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == STUB+0x10:
            assert reg(1) == device
            calls.append({'call': 'get_physical_device', 'wdf_device': hex(device), 'pdo': hex(device+0x100)})
            u.reg_write(regs.UC_ARM64_REG_X0, device+0x100)
        elif address == STUB+0x20:
            assert reg(0) == device+0x100
            description = bytes(u.mem_read(reg(1), 64))
            assert struct.unpack_from('<I', description)[0] == 3
            assert list(description[4:12]) == [1, 0, 0, 0, 0, 1, 0, 0]
            fields = struct.unpack_from('<10I', description, 12)
            assert fields == (0, 0, 17, 0, 0, 0x1234, 0, 64, 0, 0)
            assert struct.unpack_from('<Q', description, 56)[0] == 0
            calls.append({'call': 'IoGetDmaAdapter', 'description': description.hex(),
                          'decoded': {'version': 3, 'master': True, 'ignore_count': True,
                                      'dma64_legacy_flag': False, 'interface_type': 17,
                                      'maximum_length': 0x1234, 'dma_address_width': 64}})
            u.reg_write(regs.UC_ARM64_REG_X0, ADAPTER)
        elif address == STUB+0x30:
            assert reg(0) == ADAPTER and reg(3) == 0x1234 and reg(4) == 0 and reg(6) == 0
            limits = [struct.unpack('<Q', u.mem_read(reg(i), 8))[0] for i in (1, 2)]
            assert limits == [lower, upper] and reg(7) == CTX+0x80
            # Microsoft declares CacheType as an INPUT override, not output.
            # Original stp wzr,wzr at RVA 0x3b80 initializes this stack slot.
            assert reg(5) and struct.unpack('<I', u.mem_read(reg(5), 4))[0] == 0
            calls.append({'call': 'AllocateCommonBufferWithBounds', 'minimum': limits[0],
                          'maximum': limits[1], 'length': reg(3),
                          'cache_type_input': 0, 'cache_policy': 'MmNonCached'})
            u.mem_write(reg(7), struct.pack('<Q', logical))
            u.reg_write(regs.UC_ARM64_REG_X0, BUFFER if allocated else 0)
        elif address == STUB+0x40:
            assert reg(0) == BUFFER
            calls.append({'call': 'MmGetPhysicalAddress'})
            u.reg_write(regs.UC_ARM64_REG_X0, 0x12345678000)

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_X1, device)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.emu_start(BASE+0x3b24, BASE+0x3da8, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == BASE+0x3da8
    return u.reg_read(regs.UC_ARM64_REG_X22) & 0xffffffff, calls, {
        hex(offset): struct.unpack('<Q', u.mem_read(CTX+offset, 8))[0]
        for offset in (0x60, 0x78, 0x80, 0x88)}


def dispatch_device(pe):
    from emulate_sroot_image import machine, call, BUFFER as module_buffer
    from emulate_sroot_image import REQUEST as module_handle, STUB as image_stub, TABLE as image_table
    u = machine(pe)
    device = module_handle+0x8000
    u.mem_write(module_buffer+0x38, struct.pack('<Q', device))
    u.mem_write(image_table+0x610, struct.pack('<Q', image_stub+0x10))
    seen = []

    def hook(_, address, size, user):
        if address == image_stub+0x10:
            assert u.reg_read(regs.UC_ARM64_REG_X1) == module_handle
            assert u.reg_read(regs.UC_ARM64_REG_X2) == 0
            seen.append('WdfMemoryGetBuffer')
            u.reg_write(regs.UC_ARM64_REG_X0, module_buffer)

    u.hook_add(UC_HOOK_CODE, hook)
    call(u, 0x321e0, (module_handle, 0, 0x1234, 0x150214, 0, 0, 0, 0), end=BASE+0x31c98)
    assert seen == ['WdfMemoryGetBuffer']
    assert u.reg_read(regs.UC_ARM64_REG_X0) == device
    assert u.reg_read(regs.UC_ARM64_REG_X1) == 0x1234
    return device


def main():
    path = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HASH
    pe = pefile.PE(str(path))
    imports = {item.address: item.name for entry in pe.DIRECTORY_ENTRY_IMPORT for item in entry.imports}
    assert imports[BASE+0x191a8] == b'IoGetDmaAdapter'
    assert imports[BASE+0x191a0] == b'MmGetPhysicalAddress'
    device = dispatch_device(pe)
    chained_status, chained_calls, _ = run(pe, device=device)
    assert chained_status == 0 and chained_calls[0]['wdf_device'] == hex(device)
    rows = []
    for logical in (0x40000000, 0x40010000, 0x4fffffff, 0x3fffffff, 0x50000000):
        status, calls, fields = run(pe, logical)
        assert status == (0 if 0x40000000 <= logical <= 0x4fffffff else 0xc0000141)
        assert fields == {'0x60': ADAPTER, '0x78': BUFFER, '0x80': logical, '0x88': 0x12345678000}
        rows.append({'logical': hex(logical), 'status': hex(status), 'calls': calls, 'context': fields})
    status, calls, fields = run(pe, allocated=False)
    assert status == 0xc000000d and len(calls) == 3 and fields['0x78'] == 0 and fields['0x88'] == 0
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_slice': '0x3b24..0x3da8',
                      'cases': rows, 'allocation_failure_checked': True,
                      'dispatch_device_to_dma_chain': {'wdf_device': hex(device), 'calls': chained_calls},
                      'limitations': ['All OS and DMA allocation operations are stubs',
                                      'CacheType input is MmNonCached; allocation/OS callbacks remain synthetic',
                                      'This validates logical versus CPU-physical distinction, not live IOMMU mapping']}, indent=2))


if __name__ == '__main__':
    main()
