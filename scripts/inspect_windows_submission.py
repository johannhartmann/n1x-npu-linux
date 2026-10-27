#!/usr/bin/env python3
"""Execute runtime submission through original cuDLA WDDM envelope builders.

Offline only. Device/sync objects, command construction, OS locks and D3DKMT
are modeled. No claim about kernel retirement or physical DMA quiescence.
"""
import hashlib
import json
import struct

import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_diagnostic_contract import ROOT, RUNTIME_HASH
from inspect_cudla_release import SHA256 as CUDLA_HASH

BASE, CBASE = 0x180000000, 0x190000000
RAM, STACK, STOP = 0x44000000, 0x45000000, 0x46000000
CTX, _UNUSED, DEVICE, DVT, KMI, SYNC, SVT, HEAD, NODE, PRIVATE = (
    RAM+i*0x1000 for i in range(10))
TASK = RAM+0x10000
KEY, CONTEXT, COMMAND = 0x123456780, 0x55, 0x234567890
WAIT = 'D3DKMTWaitForSynchronizationObjectFromGpu'
SUBMIT = 'D3DKMTSubmitCommand'
SIGNAL = 'D3DKMTSignalSynchronizationObjectFromGpu2'


def image(name, digest, base):
    path = ROOT/'sources/nvidia/Display.Driver'/name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    pe = pefile.PE(str(path))
    pe.relocate_image(base)
    return pe


class Machine:
    def __init__(self, runtime, cudla, pre=(), post=(), ring=0, failure=None):
        self.u = u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for base, pe in ((BASE, runtime), (CBASE, cudla)):
            u.mem_map(base, (pe.OPTIONAL_HEADER.SizeOfImage+4095)&~4095)
            u.mem_write(base, pe.get_memory_mapped_image())
        for addr, size in ((RAM, 0x40000), (STACK, 0x10000), (STOP, 0x1000)):
            u.mem_map(addr, size)
        self.events, self.resolved, self.imports = [], {}, {}
        self.failure, self.counter, self.ring = failure, 100, ring
        self.command_address = COMMAND
        self.sync_type = 6
        self.put(BASE+0xa5340, STOP+0x10)  # CFG validation boundary
        for dll in cudla.DIRECTORY_ENTRY_IMPORT:
            for entry in dll.imports:
                target = STOP+0x100+4*len(self.imports)
                assert target < STOP+0x800
                self.imports[target] = entry.name.decode() if entry.name else str(entry.ordinal)
                self.put(entry.address, target)
        self.put(CTX, KEY)
        self.put(CTX+0x10, CONTEXT)
        self.put(CTX+0x20, PRIVATE)
        self.put(CTX+0x28, 0x354)
        self.put(CTX+0x630, ring, '<I')
        self.put(CTX+(ring+1)*0x30+0x28, SYNC)
        self.put(DEVICE, DVT)
        self.put(DVT+0x18, STOP+0x14)
        self.put(KMI, CBASE+0xc2078)
        self.put(SYNC, SVT)
        for slot, target in ((0, 0x20), (8, 0x24), (0x18, 0x28),
                             (0x40, 0x2c), (0x48, 0x30)):
            self.put(SVT+slot, STOP+target)
        self.put(BASE+0x6a3d10, HEAD)
        for offset in (0, 8, 16):
            self.put(HEAD+offset, NODE)
            self.put(NODE+offset, HEAD)
        self.put(HEAD+24, 0x101, '<H')
        self.put(NODE+24, 1, '<H')
        self.put(NODE+32, KEY)
        self.put(NODE+40, DEVICE)
        assert len(pre) <= 31 and len(post) <= 31
        self.put(TASK+8, len(pre), '<B')
        self.put(TASK+9, len(post), '<B')
        for start, rows in ((0x18, pre), (0xa18, post)):
            for index, (kind, subtype, handle, value) in enumerate(rows):
                ptr = TASK+start+index*0x50
                self.put(ptr, kind, '<B')
                self.put(ptr+1, subtype, '<B')
                self.put(ptr+0x28, handle, '<I')
                self.put(ptr+0x48, value, '<I')
        u.mem_write(PRIVATE, b'\xa5'*0x354)
        u.hook_add(UC_HOOK_CODE, self.hook)
        assert self.call(CBASE+0xb8b8) == 1
        for rva, name in ((0xcfac0, WAIT), (0xcfb30, SUBMIT), (0xcfa58, SIGNAL)):
            assert self.resolved[self.get(CBASE+rva)] == name

    def put(self, addr, value, fmt='<Q'):
        self.u.mem_write(addr, struct.pack(fmt, value))

    def get(self, addr, fmt='<Q'):
        return struct.unpack(fmt, self.u.mem_read(addr, struct.calcsize(fmt)))[0]

    def reg(self, index):
        return self.u.reg_read(getattr(r, f'UC_ARM64_REG_X{index}'))

    def ret(self, value=0):
        self.u.reg_write(r.UC_ARM64_REG_X0, value)
        self.u.reg_write(r.UC_ARM64_REG_PC, self.reg(30))

    def array(self, addr, count, fmt):
        assert 0 < count <= 32
        return [self.get(addr+i*struct.calcsize(fmt), fmt) for i in range(count)]

    def hook(self, u, pc, size, _):
        if pc == STOP+0x10:
            # CFG check preserves the pending call's argument registers.
            u.reg_write(r.UC_ARM64_REG_PC, self.reg(30))
        elif pc in self.imports:
            name = self.imports[pc]
            if name in ('GetModuleHandleA', 'LoadLibraryA'):
                self.ret(0x1234)
            elif name == 'GetProcAddress':
                assert self.reg(0) == 0x1234
                name = bytes(u.mem_read(self.reg(1), 128)).split(b'\0')[0].decode()
                target = STOP+0x800+4*len(self.resolved)
                assert target < STOP+0x1000
                self.resolved[target] = name
                self.ret(target)
            else:
                raise AssertionError(('unexpected import', name))
        elif pc in (BASE+0x6b290, BASE+0x6b2d8):
            assert self.reg(0) == BASE+0x6a3d30
            self.ret()
        elif pc in (CBASE+0x46d98, CBASE+0x46e20):
            assert self.reg(0) in (CBASE+0xcdae0, KMI+0x18)
            self.ret()
        elif pc == STOP+0x14:
            assert self.reg(0) == DEVICE
            self.ret(KMI)
        elif STOP+0x20 <= pc <= STOP+0x30:
            assert self.reg(0) == SYNC
            if pc == STOP+0x20:
                self.put(self.reg(1), self.sync_type, '<B')
                self.ret(self.reg(1))
            elif pc == STOP+0x24:
                assert self.reg(1) == 0
                self.events.append({'boundary': 'sync_slot_08'})
                self.ret(11 if self.failure == 'sync_slot_08' else 0)
            elif pc == STOP+0x28:
                self.put(self.reg(1), self.counter, '<I')
                self.ret()
            elif pc == STOP+0x2c:
                self.put(self.reg(1)+0x10, 0x99, '<I')
                self.put(self.reg(1)+0x30, self.counter, '<I')
                self.ret(11 if self.failure == 'sync_descriptor' else 0)
            else:
                self.counter = self.reg(1)&0xffffffff
                self.events.append({'boundary': 'set_sync_value', 'value': self.counter})
                self.ret()
        elif pc == BASE+0x743a8:
            self.build_command()
        elif pc in self.resolved:
            name, ptr = self.resolved[pc], self.reg(0)
            event = {'api': name}
            if name == WAIT:
                assert self.get(ptr, '<I') == CONTEXT
                count = self.get(ptr+4, '<I')
                event['handles'] = self.array(self.get(ptr+8), count, '<I')
                event['values'] = self.array(self.get(ptr+16), count, '<Q')
            elif name == SUBMIT:
                assert self.get(ptr) == self.command_address and self.get(ptr+8, '<I') == 0x40
                assert self.get(ptr+0x18, '<I') == 1
                assert self.get(ptr+0x1c, '<I') == CONTEXT
                assert self.get(ptr+0x120) == PRIVATE and self.get(ptr+0x128, '<I') == 0x354
                blob = bytes(u.mem_read(PRIVATE, 0x354))
                assert blob == struct.pack('<QII', 0x100044e564441, 0, 0x354)+bytes(0x344)
                event['private_header'] = blob[:16].hex()
                event['private_bytes'] = len(blob)
            elif name == SIGNAL:
                count = self.get(ptr, '<I')
                event['handles'] = self.array(self.get(ptr+8), count, '<I')
                assert self.get(ptr+16, '<I') == 0  # flags
                assert self.get(ptr+20, '<I') == 1  # context count
                assert self.get(self.get(ptr+24), '<I') == CONTEXT
                event['values'] = self.array(self.get(ptr+32), count, '<Q')
            else:
                raise AssertionError(('unexpected OS boundary', name))
            self.events.append(event)
            self.ret(0xc0000001 if self.failure == name else 0)

    def build_command(self):
        assert [self.reg(i) for i in range(4)] == [TASK, 1, CTX+(self.ring+1)*0x30, 7]
        self.events.append({'boundary': 'modeled_command_builder'})
        self.put(self.reg(4), self.command_address)
        self.put(self.reg(5), 0x40)
        self.ret(2 if self.failure == 'builder' else 0)

    def call(self, address, *args):
        for index, value in enumerate(args):
            self.u.reg_write(getattr(r, f'UC_ARM64_REG_X{index}'), value)
        self.u.reg_write(r.UC_ARM64_REG_SP, STACK+0xf000)
        self.u.reg_write(r.UC_ARM64_REG_LR, STOP)
        self.u.emu_start(address, STOP, count=100000)
        assert self.u.reg_read(r.UC_ARM64_REG_PC) == STOP
        return self.reg(0)&0xffffffff


def main():
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, BASE)
    cudla = image('nvcudla.dll', CUDLA_HASH, CBASE)
    for slot, target in ((0x80, 0x97c0), (0x88, 0x9938), (0xc0, 0x9f78)):
        assert struct.unpack('<Q', cudla.get_data(0xc2078+slot, 8))[0] == CBASE+target
    rows = []
    def run(name, pre=(), post=(), ring=0, failure=None, count=1,
            status=0, apis=(SUBMIT, SIGNAL), counter=101, next_ring=None):
        m = Machine(runtime, cudla, pre, post, ring, failure)
        result = m.call(BASE+0x75858, CTX, TASK, count, 7)
        assert result == status, (name, hex(result), hex(status))
        assert [e['api'] for e in m.events if 'api' in e] == list(apis), (name, m.events)
        for event in m.events:
            if event.get('api') == WAIT:
                selected = [row for row in pre if row[1] == 0]
                assert event['handles'] == [row[2] for row in selected]
                assert event['values'] == [row[3] for row in selected]
            elif event.get('api') == SIGNAL:
                assert event['handles'] == [row[2] for row in post]+[0x99]
                assert event['values'] == [row[3] for row in post]+[101]
        assert m.counter == counter, (name, m.counter)
        actual_ring = m.get(CTX+0x630, '<I')
        expected_ring = ((ring+1)&31 if status == 0 else ring) if next_ring is None else next_ring
        assert actual_ring == expected_ring, (name, actual_ring)
        rows.append({'name': name, 'status': hex(result), 'ring': actual_ring,
                     'sync_value': m.counter, 'events': m.events})
        return m
    m = run('pre-submit-post', [(4, 0, 0x77, 0xffffffff)], [(4, 1, 0x88, 0x80000001)],
            apis=(WAIT, SUBMIT, SIGNAL))
    assert next(e for e in m.events if e.get('api') == WAIT)['values'] == [0xffffffff]
    signal = next(e for e in m.events if e.get('api') == SIGNAL)
    assert signal['handles'] == [0x88, 0x99] and signal['values'] == [0x80000001, 101]
    run('internal-fence-only')
    m = run('31-pre-and-post-fences', [(4, 0, 0x100+i, i) for i in range(31)],
            [(4, 1, 0x200+i, i+1) for i in range(31)], apis=(WAIT, SUBMIT, SIGNAL))
    signal = next(e for e in m.events if e.get('api') == SIGNAL)
    assert signal['handles'] == list(range(0x200, 0x21f))+[0x99]
    assert signal['values'] == list(range(1, 32))+[101]
    run('ring-wrap', ring=31)
    run('skip-nonzero-pre-subtype', [(4, 1, 0x77, 42)])
    run('invalid-count', count=2, status=2, apis=(), counter=100)
    run('sync-slot-08-error', failure='sync_slot_08', status=11, apis=(), counter=100)
    run('bad-pre-kind', [(3, 0, 0x77, 42)], status=2, apis=(), counter=100)
    run('wait-error', [(4, 0, 0x77, 42)], failure=WAIT,
        status=0x3000f, apis=(WAIT,), counter=100)
    run('builder-error', failure='builder', status=2, apis=(), counter=100)
    run('submit-error', failure=SUBMIT, status=0x3000f, apis=(SUBMIT,), counter=100)
    run('post-signal-error', failure=SIGNAL, status=0x3000f,
        apis=(SUBMIT, SIGNAL), counter=100)
    run('bad-post-subtype-after-submit', post=[(4, 0, 0x88, 42)],
        status=2, apis=(SUBMIT,), counter=100)
    run('sync-descriptor-error-after-submit', failure='sync_descriptor',
        status=11, apis=(SUBMIT,), counter=100)
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'runtime_sha256': RUNTIME_HASH, 'cudla_sha256': CUDLA_HASH,
                      'runtime_submit_rva': '0x75858', 'runtime_post_signal_rva': '0x745a0',
                      'cases': rows,
                      'limitations': [
                          'Command builder and device/sync getters are modeled, not original implementations',
                          'OS locks, CFG validation and D3DKMT calls are modeled',
                          'Original device lookup, submission, post-signal and KMI wrappers execute',
                          'Constructed objects do not prove live Windows backend selection',
                          'No KMD completion, firmware execution or physical buffer-retirement guarantee']}, indent=2))


if __name__ == '__main__':
    main()
