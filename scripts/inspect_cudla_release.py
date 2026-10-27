#!/usr/bin/env python3
"""Execute Windows cuDLA error gating and memory-unregister instructions.

No device access. Runtime unregister/destroy and Windows initialization/locking
are explicit modeled boundaries, not evidence of DMA quiescence.
"""
import hashlib
import json
import struct
from pathlib import Path

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

ROOT = Path(__file__).resolve().parents[1]
SHA256 = '8da1b42d35aa79c71e3102f95dde30a75dbe498983d386b3cb68082f6b467ba0'
BASE = 0x180000000
RAM, STACK, STOP = 0x41000000, 0x42000000, 0x43000000
CTX, HANDLE, ADAPTER, RUNTIME, RVTABLE = (RAM + i*0x1000 for i in range(5))
HEAD, NODE, BLOCK, SHARED_HEAD, SHARED_NODE = (RAM + i*0x1000 for i in range(5, 10))
MEMORY, KEY = RAM+0xa000, 0x123456780
GPU_OBJECT, GPU_VTABLE, KMI, ADAPTER_INFO = (RAM+i*0x1000 for i in range(12, 16))


class Machine:
    def __init__(self, pe, statuses=(0,)*5, unregister_status=0, kind=0):
        self.u = u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        u.mem_map(BASE, (pe.OPTIONAL_HEADER.SizeOfImage+4095)&~4095)
        u.mem_write(BASE, pe.get_memory_mapped_image())
        for address, size in ((RAM, 0x20000), (STACK, 0x10000), (STOP, 4096)):
            u.mem_map(address, size)
        self.events = []
        self.kernel_envelopes = []
        self.resolved = {}
        self.fail_api = None
        self.status = unregister_status
        self.put(HANDLE, CTX)
        self.put(CTX, BASE+0xc2628)
        self.put(CTX+0x38, 0x11a9a, '<I')
        self.put(CTX+0x18, ADAPTER)
        self.put(ADAPTER, BASE+0xc2820)
        self.put(ADAPTER+0x10, RUNTIME)
        self.put(ADAPTER+0x18, 1, '<B')
        self.put(RUNTIME, RVTABLE)
        self.put(RVTABLE+0xa8, STOP+0x20)
        self.put(RVTABLE+0x100, STOP+0x24)
        self.put(BASE+0xce0b0, STOP+0x28)  # unregisterTaskStatistics
        self.put(BASE+0xce0f0, STOP+0x2c)  # destroyMemory
        self.imports = {}
        for dll in pe.DIRECTORY_ENTRY_IMPORT:
            for entry in dll.imports:
                target = STOP+0x100+4*len(self.imports)
                assert target < STOP+0x800
                self.imports[target] = entry.name.decode() if entry.name else str(entry.ordinal)
                self.put(entry.address, target)
        for index, status in enumerate(statuses):
            ptr = RAM+0xb000+32*index
            self.put(CTX+0x40+8*index, ptr)
            self.put(ptr+14, status, '<H')
        self.tree(CTX+0x110, HEAD, NODE, KEY)
        self.put(NODE+0x28, BLOCK)
        self.put(NODE+0x30, kind, '<I')
        self.put(BLOCK, MEMORY)
        self.put(BLOCK+0x20, 0x100)
        self.put(BLOCK+0x30, KEY+0x100)
        self.tree(ADAPTER+0x40, SHARED_HEAD, SHARED_NODE, KEY+0x100)
        self.put(SHARED_NODE+0x30, 2, '<I')  # another mapping still owns the backing
        u.hook_add(UC_HOOK_CODE, self.hook)

    def put(self, address, value, fmt='<Q'):
        self.u.mem_write(address, struct.pack(fmt, value))

    def get(self, address, fmt='<Q'):
        return struct.unpack(fmt, self.u.mem_read(address, struct.calcsize(fmt)))[0]

    def tree(self, owner, head, node, key):
        self.put(owner, head)
        self.put(owner+8, 1)
        for offset in (0, 8, 16):
            self.put(head+offset, node)
            self.put(node+offset, head)
        self.put(head+24, 0x101, '<H')
        self.put(node+24, 1, '<H')
        self.put(node+32, key)

    def reg(self, i):
        return self.u.reg_read(getattr(r, f'UC_ARM64_REG_X{i}'))

    def ret(self, value=0):
        self.u.reg_write(r.UC_ARM64_REG_X0, value)
        self.u.reg_write(r.UC_ARM64_REG_PC, self.reg(30))

    def hook(self, u, pc, size, _):
        if pc in self.imports:
            name = self.imports[pc]
            if name == 'InitOnceBeginInitialize':
                self.put(self.reg(2), 0, '<I')  # already initialized
                self.ret(1)
            elif name in ('EnterCriticalSection', 'LeaveCriticalSection'):
                self.events.append(name)
                self.ret()
            elif name in ('LoadLibraryA', 'GetModuleHandleA'):
                self.ret(0x1234)
            elif name == 'GetProcAddress':
                assert self.reg(0) == 0x1234
                name = bytes(u.mem_read(self.reg(1), 128)).split(b'\0')[0].decode()
                target = STOP+0x800+4*len(self.resolved)
                assert target < STOP+4096
                self.resolved[target] = name
                self.ret(target)
            else:
                raise AssertionError(('unexpected import', name, hex(pc)))
        elif pc in (BASE+0x46d98, BASE+0x46e20):
            # Only the dynamic API resolver uses these CRT mutex helpers.
            assert self.reg(0) == BASE+0xcdae0
            self.ret()
        elif pc == STOP+0x30:
            assert self.reg(0) == GPU_OBJECT
            self.ret(0x200000000)
        elif pc in self.resolved:
            name = self.resolved[pc]
            ptr = self.reg(0)
            if name == 'D3DKMTEvict':
                assert self.get(ptr, '<I') == 0x55
                assert self.get(ptr+4, '<I') == 1
                assert self.get(self.get(ptr+8), '<I') == 0x123
                assert self.get(ptr+16, '<I') == 0
            elif name == 'D3DKMTFreeGpuVirtualAddress':
                assert self.get(ptr, '<I') == 0x77
                assert self.get(ptr+8) == 0x200000000
                assert self.get(ptr+16) == 0x2000
            elif name == 'D3DKMTDestroyAllocation2':
                assert bytes(u.mem_read(ptr, 24)) == struct.pack('<IIQII', 0x55, 0x456, 0, 0, 0)
            else:
                raise AssertionError(('unexpected kernel boundary', name))
            self.kernel_envelopes.append({'api': name,
                'bytes': bytes(u.mem_read(ptr, 32 if name == 'D3DKMTEvict' else 24)).hex()})
            self.events.append(name)
            self.ret(0xc0000001 if self.fail_api == name else 0)
        elif pc in (STOP+0x20, STOP+0x24, STOP+0x28):
            assert self.reg(0) == RUNTIME and self.reg(1) == MEMORY
            assert self.reg(2) == 0x100
            if pc == STOP+0x20:
                assert self.reg(3) == 0
            self.events.append({'runtime_unregister_slot': hex(pc-STOP),
                                'return_status': self.status})
            self.ret(self.status)
        elif pc == STOP+0x2c:
            assert self.reg(0) == MEMORY
            self.events.append('runtime_destroy_memory')
            self.ret()
        elif pc == BASE+0x42398:
            ptr = self.reg(0)
            names = {BLOCK: 'free_block', NODE: 'free_registration',
                     SHARED_NODE: 'free_shared_record', GPU_OBJECT: 'free_gpu_object'}
            assert ptr in names and self.reg(1) == (0x48 if ptr == SHARED_NODE else 0x38)
            self.events.append(names[ptr])
            self.ret()

    def last_reference(self):
        # Execute the original resolver, retaining exact API-name/pointer stores.
        assert self.call(0xb8b8) == 1
        for rva, name in ((0xcfa88, 'D3DKMTEvict'),
                          (0xcfb00, 'D3DKMTFreeGpuVirtualAddress'),
                          (0xcfb40, 'D3DKMTDestroyAllocation2')):
            assert self.resolved[self.get(BASE+rva)] == name
        self.put(SHARED_NODE+0x30, 1, '<I')
        self.put(SHARED_NODE+0x34, 0x123, '<I')
        self.put(SHARED_NODE+0x38, 0x456, '<I')
        self.put(SHARED_NODE+0x40, 0x2000)
        self.put(ADAPTER+0x20, KMI)
        self.put(KMI, BASE+0xc2078)
        self.put(KMI+8, ADAPTER_INFO)
        self.put(KMI+16, 0x55, '<I')
        self.put(ADAPTER_INFO+4, 0x77, '<I')
        self.put(BLOCK+8, GPU_OBJECT)
        self.put(GPU_OBJECT, GPU_VTABLE)
        self.put(GPU_VTABLE+8, STOP+0x30)

    def call(self, rva, *args):
        for index, arg in enumerate(args):
            self.u.reg_write(getattr(r, f'UC_ARM64_REG_X{index}'), arg)
        self.u.reg_write(r.UC_ARM64_REG_SP, STACK+0xf000)
        self.u.reg_write(r.UC_ARM64_REG_LR, STOP)
        self.u.emu_start(BASE+rva, STOP, count=100000)
        assert self.u.reg_read(r.UC_ARM64_REG_PC) == STOP
        return self.reg(0)&0xffffffff


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvcudla.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA256
    pe = pefile.PE(str(path))
    delayed = {entry.address: entry.name.decode()
               for dll in pe.DIRECTORY_ENTRY_DELAY_IMPORT for entry in dll.imports}
    assert delayed[BASE+0xce0b0] == '?unregisterTaskStatistics@IRuntime@nvdla@@QEAA?AW4NvError@@UNvDlaMemDescRec@@@Z'
    assert delayed[BASE+0xce0f0] == '?destroyMemory@nvdla@@YAXQEAVIMemory@1@@Z'
    # Prove the release path slots in the concrete tables from the factory.
    for table, slot, target in ((0xc2628, 0x20, 0x20560),
                                (0xc2820, 0x20, 0x2d000),
                                (0xc2820, 0x40, 0x2dec0),
                                (0xc2820, 0x48, 0x2dee8)):
        assert struct.unpack('<Q', pe.get_data(table+slot, 8))[0] == BASE+target
    mapping = {0: 0, 3: 0x40000001, 6: 0x40000002, 8: 0x40000003,
               12: 0x40000004, 14: 0x40000005, 15: 0x40000006, 16: 0x40000007}
    gate_cases = 0
    for position in range(5):
        for status in (*range(18), 0x100, 0xffff):
            values = [0]*5
            values[position] = status
            m = Machine(pe, values)
            assert m.call(0x273b0, CTX) == mapping.get(status, 7)
            assert not m.events
            gate_cases += 1
    # Status 14 is remembered but scanning continues; other nonzero statuses
    # return immediately. This is not max/severity aggregation.
    for values, expected in (((14, 0, 0, 0, 0), 0x40000005),
                             ((14, 0, 3, 0, 0), 0x40000001),
                             ((3, 0, 14, 0, 0), 0x40000001),
                             ((14, 0, 1, 0, 0), 7)):
        assert Machine(pe, values).call(0x273b0, CTX) == expected
        gate_cases += 1
    release_cases = []
    cases = [('success', (0,)*5, 0, KEY, HANDLE, 0),
             ('statistics', (0,)*5, 0, KEY, HANDLE, 2),
             ('async-error', (0, 0, 3, 0, 0), 0, KEY, HANDLE, 0),
             ('remembered-error', (0, 0, 14, 0, 0), 0, KEY, HANDLE, 0),
             ('runtime-error', (0,)*5, 11, KEY, HANDLE, 0),
             ('unknown-buffer', (0,)*5, 0, KEY+1, HANDLE, 0),
             ('null-buffer', (0,)*5, 0, 0, HANDLE, 0),
             ('null-device', (0,)*5, 0, KEY, 0, 0)]
    expected = [0, 0, 0x40000001, 0x40000005, 7, 4, 4, 8]
    for (name, values, runtime_error, key, handle, kind), wanted in zip(cases, expected):
        m = Machine(pe, values, runtime_error, kind)
        status = m.call(0xdc20, handle, key)
        assert status == wanted, (name, status, wanted)
        assert m.events[0] == 'EnterCriticalSection'
        assert m.events[-1] == 'LeaveCriticalSection'
        body = m.events[1:-1]
        if wanted == 0:
            assert body == [{'runtime_unregister_slot': '0x28' if kind == 2 else '0x20',
                             'return_status': 0}, 'runtime_destroy_memory',
                            'free_block', 'free_registration'], (name, body)
            assert m.get(CTX+0x118) == 0
            assert m.get(SHARED_NODE+0x30, '<I') == 1
        else:
            assert m.get(CTX+0x118) == 1
            assert m.get(SHARED_NODE+0x30, '<I') == 2
            assert body == ([{'runtime_unregister_slot': '0x20',
                              'return_status': 11}] if name == 'runtime-error' else [])
        release_cases.append({'name': name, 'status': hex(status), 'events': m.events,
                              'remaining_registrations': m.get(CTX+0x118),
                              'shared_backing_references': m.get(SHARED_NODE+0x30, '<I')})
    kernel_apis = ['D3DKMTEvict', 'D3DKMTFreeGpuVirtualAddress', 'D3DKMTDestroyAllocation2']
    for fail_api in (None, *kernel_apis):
        m = Machine(pe)
        m.last_reference()
        m.fail_api = fail_api
        status = m.call(0xdc20, HANDLE, KEY)
        assert status == (0x7fffffff if fail_api else 0), (fail_api, status)
        calls = [item for item in m.events if isinstance(item, str) and item.startswith('D3DKMT')]
        expected_calls = kernel_apis[:kernel_apis.index(fail_api)+1] if fail_api else kernel_apis
        assert calls == expected_calls
        assert m.get(ADAPTER+0x48) == 0
        assert m.get(CTX+0x118) == (1 if fail_api else 0)
        assert 'free_shared_record' in m.events and 'free_block' in m.events
        assert ('free_registration' in m.events) == (fail_api is None)
        release_cases.append({'name': 'last-reference', 'injected_failure': fail_api,
                              'status': hex(status), 'events': m.events,
                              'kernel_envelopes': m.kernel_envelopes,
                              'remaining_registrations': m.get(CTX+0x118),
                              'remaining_shared_records': m.get(ADAPTER+0x48)})
    print(json.dumps({'passed': True, 'hardware_access': False, 'sha256': SHA256,
                      'error_gate_rva': '0x273b0', 'gate_cases': gate_cases,
                      'unregister_export_rva': '0xdc20', 'backend_rva': '0x20560',
                      'release_cases': release_cases,
                      'limitations': [
                          'Modeled initialized Windows locks and runtime unregister/destroy calls',
                          'Constructed registration trees with one or two backing references',
                          'Original KMI envelopes execute; D3DKMT itself is modeled',
                          'No actual KMD free, task execution, fence or DMA ordering proof',
                          'The alternate backend at RVA 0x134c8 is not executed',
                          'Does not establish which backend is selected on a live Windows N1X']}, indent=2))


if __name__ == '__main__':
    main()
