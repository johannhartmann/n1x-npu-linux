#!/usr/bin/env python3
"""Run the Windows runtime command builder and its helpers in RAM only.

Original code resolves constructed registry entries, builds address/action
streams and packs the command descriptor. Memory-object virtual methods,
mutexes and CFG checks are explicit modeled boundaries. No driver or device IO.
"""
import json
import struct

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

from inspect_windows_submission import (Machine, image, BASE, CBASE, RAM, STACK,
                                       STOP, TASK, CTX, SYNC, WAIT, SUBMIT, SIGNAL,
                                       CUDLA_HASH)
from inspect_diagnostic_contract import RUNTIME_HASH

MEM, REF1, REF2, VT = (RAM+v for v in (0x30000, 0x30100, 0x30200, 0x31000))
SLOT, OWNER, OUT = (RAM+v for v in (0x32000, 0x32100, 0x32200))
CPU = RAM+0x40000
GPU, GPU1, GPU2 = 0x12345678000, 0x23456789000, 0x3456789a000
DESC, ADDRS, PRE, POST = 0x80, 0x100, 0x1000, 0x2000
SENTINEL = 0xa5a5a5a5a5a5a5a5
# task count byte, task array offset, emitted opcode
STREAMS = ((0xa, 0x1418, 0xc0), (0xb, 0x1618, 0xc1),
           (0xc, 0x1818, 0xc1), (0xd, 0x1a18, 0x87),
           (0xe, 0x1c18, 0x87))


class Builder(Machine):
    def __init__(self, runtime, map_fail=0, unmap_fail=0):
        self.u = u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        u.mem_map(BASE, (runtime.OPTIONAL_HEADER.SizeOfImage+4095)&~4095)
        u.mem_write(BASE, runtime.get_memory_mapped_image())
        for addr, size in ((RAM, 0x60000), (STACK, 0x10000), (STOP, 0x1000)):
            u.mem_map(addr, size)
        self.configure(map_fail, unmap_fail)
        u.hook_add(UC_HOOK_CODE, self.hook)

    def configure(self, map_fail=0, unmap_fail=0):
        self.events, self.visited = [], set()
        self.map_fail, self.unmap_fail = map_fail, unmap_fail
        self.maps = self.unmaps = 0
        self.kinds = {MEM: 1, REF1: 1, REF2: 2}
        self.gpu = {MEM: GPU, REF1: GPU1, REF2: GPU2}
        self.put(BASE+0xa5340, STOP+0x10)
        for obj in self.kinds:
            self.put(obj, VT)
        for offset, target in ((0, 0x40), (0x10, 0x44), (0x18, 0x48), (0x38, 0x4c)):
            self.put(VT+offset, STOP+target)
        # These are lookup registries, not RTTI casts. Execute the original
        # three lookup routines against small valid sentinel/binary trees.
        for i, (global_rva, objects) in enumerate(((0x6a3da0, [MEM, REF1, REF2]),
                                                 (0x6a3dc0, [MEM, REF1]),
                                                 (0x6a3d80, [REF2]))):
            head = RAM+0x38000+i*0x1000
            self.put(BASE+global_rva, head)
            for offset in (0, 8, 16):
                self.put(head+offset, head+0x100)
            self.put(head+0x19, 1, '<B')
            for n, obj in enumerate(objects):
                node = head+0x100+n*0x80
                self.put(node, head)
                self.put(node+8, head if n == 0 else node-0x80)
                self.put(node+16, node+0x80 if n+1 < len(objects) else head)
                self.put(node+0x20, obj)
                self.put(node+0x28, obj)
        self.put(OWNER+0x18, MEM)
        for offset, value in enumerate((OWNER, DESC, ADDRS, PRE, POST, 0)):
            self.put(SLOT+offset*8, value)
        self.put(OUT, SENTINEL)
        self.put(OUT+8, SENTINEL)
        self.u.mem_write(CPU, b'\xa5'*0x4000)

    def hook(self, u, pc, size, _):
        if BASE <= pc < BASE+0x80000:
            self.visited.add(pc-BASE)
        if pc == STOP+0x10:
            u.reg_write(r.UC_ARM64_REG_PC, self.reg(30))
        elif pc in (BASE+0x6b290, BASE+0x6b2d8):
            assert self.reg(0) == BASE+0x6a3de0
            self.ret()
        elif pc == STOP+0x40:
            self.put(self.reg(1), self.kinds[self.reg(0)], '<B')
            self.ret(self.reg(1))
        elif pc == STOP+0x44:
            assert self.reg(0) == MEM and self.reg(1) == 2
            self.maps += 1
            error = 11 if self.maps == self.map_fail else 0
            if not error:
                self.put(self.reg(2), CPU)
            self.events.append({'map': self.maps, 'status': error})
            self.ret(error)
        elif pc == STOP+0x48:
            assert self.reg(0) == MEM and self.reg(1) in (CPU, 0)
            self.unmaps += 1
            error = 12 if self.unmaps == self.unmap_fail else 0
            self.events.append({'unmap': self.unmaps, 'cpu': hex(self.reg(1)), 'status': error})
            self.ret(error)
        elif pc == STOP+0x4c:
            self.ret(self.gpu[self.reg(0)])

    def addresses(self, rows):
        self.put(TASK+0x10, len(rows), '<I')
        for i, (obj, offset) in enumerate(rows):
            self.u.mem_write(TASK+0x1e18+i*16, struct.pack('<QII', obj, offset, 0xdeadbeef))

    def actions(self, count_byte, rows):
        array = next(offset for count, offset, _ in STREAMS if count == count_byte)
        assert len(rows) <= 32  # only bounded, non-overlapping constructed arrays
        self.put(TASK+count_byte, len(rows), '<B')
        for i, row in enumerate(rows):
            self.u.mem_write(TASK+array+i*16, struct.pack('<QII', *row))

    def fences(self, rows):
        assert len(rows) <= 31
        self.put(TASK+8, len(rows), '<B')
        for i, (kind, subtype, address, value) in enumerate(rows):
            ptr = TASK+0x18+i*80
            self.put(ptr, kind, '<B')
            self.put(ptr+1, subtype, '<B')
            self.put(ptr+0x40, address)
            self.put(ptr+0x48, value, '<I')

    def build(self, flags=7, field=0x12345678, count=1):
        self.put(TASK+0x19e18, field, '<I')
        return self.call(BASE+0x743a8, TASK, count, SLOT, flags, OUT, OUT+8)

    def verify(self, pre=b'\0', post=b'\0', addresses=(), flags=7, field=0x12345678,
               direct=True):
        expected = struct.pack('<BHQHQQHHI', 2, len(pre), GPU+PRE,
                               len(post), GPU+POST, GPU+ADDRS, len(addresses),
                               int(bool(flags & 255)), field)+bytes(27)
        assert len(expected) == 64
        assert bytes(self.u.mem_read(CPU+DESC, 64)) == expected
        if direct:
            assert self.get(OUT) == GPU+DESC and self.get(OUT+8) == 64
        for offset, payload, capacity in ((PRE, pre, 0x600), (POST, post, 0x500)):
            assert bytes(self.u.mem_read(CPU+offset, capacity)) == payload+bytes(capacity-len(payload))
            assert bytes(self.u.mem_read(CPU+offset+capacity, 16)) == b'\xa5'*16
        actual = bytes(self.u.mem_read(CPU+ADDRS, len(addresses)*8+8))
        assert actual == b''.join(struct.pack('<Q', a) for a in addresses)+b'\xa5'*8
        assert self.maps == self.unmaps == 4
        assert {0x743a8, 0x738f0, 0x73ed8, 0x73b68, 0x2c8b8, 0x2cc30, 0x1820} <= self.visited
        return expected.hex()


class SubmissionBuilder(Builder):
    """Original command generation and WDDM envelopes in one address space."""
    def __init__(self, runtime, cudla, pre=(), post=(), ring=0, failure=None,
                 map_fail=0):
        self.visited = set()
        Machine.__init__(self, runtime, cudla, pre, post, ring, failure)
        self.u.mem_map(RAM+0x40000, 0x20000)
        self.configure(map_fail=map_fail)
        for offset in range(0, 0x28, 8):
            self.put(CTX+(ring+1)*0x30+offset, self.get(SLOT+offset))
        assert self.get(CTX+(ring+1)*0x30+0x28) == SYNC
        self.command_address = GPU+DESC
        self.put(TASK+0x19e18, 0x12345678, '<I')

    def hook(self, u, pc, size, user):
        if BASE <= pc < BASE+0x80000:
            self.visited.add(pc-BASE)
        if (STOP+0x40 <= pc <= STOP+0x4c or
            pc in (BASE+0x6b290, BASE+0x6b2d8) and self.reg(0) == BASE+0x6a3de0):
            Builder.hook(self, u, pc, size, user)
        else:
            Machine.hook(self, u, pc, size, user)

    def build_command(self):
        assert [self.reg(i) for i in range(4)] == [TASK, 1, CTX+(self.ring+1)*0x30, 7]
        self.events.append({'boundary': 'original_command_builder'})
        # Do not change PC or registers: execute the original builder inline.


def action(opcode, address, value=0):
    return struct.pack('<BQ', opcode, address)+(struct.pack('<H', value & 0xffff) if opcode != 0x87 else b'')


def main():
    runtime = image('nvdla_runtime.dll', RUNTIME_HASH, BASE)
    cases = []

    def record(name, m, expected=0, **kwargs):
        result = m.build(**kwargs)
        assert result == expected, (name, result, expected)
        cases.append({'name': name, 'status': result, 'events': m.events})
        return m

    for flags in (0, 1, 7, 255, 256, 257):
        m = record(f'flag-low-byte-{flags}', Builder(runtime), flags=flags)
        m.verify(flags=flags)

    m = Builder(runtime)
    m.addresses([(REF1, 0), (REF2, 0xffffffff), (0, 0x80000001), (MEM, 8)])
    for count, _, opcode in STREAMS:
        m.actions(count, [(REF1, 0x80000001, 0x12345678), (REF2, 4, 0xffffffff)])
    m.fences([(4, 0, 0x777, 1), (4, 1, 0xabcdef00123, 0x80000001), (4, 2, 0x888, 2)])
    record('all-streams-two-memory-types-u32-offsets', m)
    streams = {count: action(op, GPU1+0x80000001, 0x12345678)+action(op, GPU2+4, 0xffffffff)
               for count, _, op in STREAMS}
    pre = streams[0xa]+streams[0xb]+streams[0xd]+struct.pack('<BQI', 0x80, 0xabcdef00123, 0x80000001)+b'\0'
    post = streams[0xc]+streams[0xe]+b'\0'
    cases[-1]['descriptor'] = m.verify(pre, post, (GPU1, GPU2+0xffffffff, 0x80000001, GPU+8))
    assert 0x2cd10 in m.visited
    cases[-1]['preactions'], cases[-1]['postactions'] = pre.hex(), post.hex()

    m = Builder(runtime)
    m.fences([(4, 1, GPU1+i*4, i) for i in range(31)])
    record('31-inline-prefences', m)
    m.verify(b''.join(struct.pack('<BQI', 0x80, GPU1+i*4, i) for i in range(31))+b'\0')

    for count, _, opcode in STREAMS:
        m = Builder(runtime)
        m.actions(count, [(REF2, i*8, i) for i in range(32)])
        record(f'32-actions-{count:x}', m)
        stream = b''.join(action(opcode, GPU2+i*8, i) for i in range(32))+b'\0'
        m.verify(pre=stream if count in (0xa, 0xb, 0xd) else b'\0',
                 post=stream if count in (0xc, 0xe) else b'\0')

    for nth in range(1, 5):
        m = record(f'map-failure-{nth}', Builder(runtime, map_fail=nth), expected=11)
        assert outputs_unchanged(m)
        assert any(e.get('cpu') == '0x0' for e in m.events)
        # Helper failure still unwinds its own map and the outer map.
        assert m.maps == nth and m.unmaps == (1 if nth == 1 else nth)
        m = record(f'unmap-failure-{nth}', Builder(runtime, unmap_fail=nth),
                   expected=0 if nth == 4 else 12)
        if nth == 4:
            m.verify()  # outer cleanup status is discarded by vendor builder
        else:
            assert outputs_unchanged(m)

    for region in ('addresses', *(count for count, _, _ in STREAMS)):
        for bad, expected in ((0x112233445566, 4), (REF1, 2)):
            m = Builder(runtime)
            if bad == REF1:
                m.kinds[REF1] = 3
            if region == 'addresses':
                m.addresses([(bad, 0)])
            else:
                m.actions(region, [(bad, 0, 1)])
            record(f'bad-object-{region}-{expected}', m, expected=expected)
            assert outputs_unchanged(m)
    m = Builder(runtime, unmap_fail=1)
    m.fences([(3, 1, GPU1, 42)])
    record('bad-fence-kind-preserves-prior-error', m, expected=2)
    assert outputs_unchanged(m)

    cudla = image('nvcudla.dll', CUDLA_HASH, CBASE)
    for name, ring, failure, map_fail, status, apis in (
            ('full-submit', 0, None, 0, 0, [WAIT, SUBMIT, SIGNAL]),
            ('full-submit-ring-wrap', 31, None, 0, 0, [WAIT, SUBMIT, SIGNAL]),
            ('full-submit-builder-error', 0, None, 2, 11, [WAIT]),
            ('full-submit-os-error', 0, SUBMIT, 0, 0x3000f, [WAIT, SUBMIT]),
            ('full-submit-late-signal-error', 0, SIGNAL, 0, 0x3000f, [WAIT, SUBMIT, SIGNAL])):
        m = SubmissionBuilder(runtime, cudla,
                              pre=[(4, 0, 0x77, 2), (4, 1, 0x88, 3)],
                              post=[(4, 1, 0x98, 4)], ring=ring,
                              failure=failure, map_fail=map_fail)
        m.put(TASK+0x18+80+0x40, GPU2)
        m.addresses([(REF1, 8), (REF2, 16)])
        result = m.call(BASE+0x75858, CTX, TASK, 1, 7)
        assert result == status, (name, result)
        assert [e['api'] for e in m.events if 'api' in e] == apis
        for event in m.events:
            if event.get('api') == WAIT:
                assert event['handles'] == [0x77] and event['values'] == [2]
            elif event.get('api') == SIGNAL:
                assert event['handles'] == [0x98, 0x99] and event['values'] == [4, 101]
        assert m.get(CTX+0x630, '<I') == ((ring+1)&31 if not status else ring)
        assert m.counter == (101 if not status else 100)
        if not map_fail:
            m.verify(pre=struct.pack('<BQI', 0x80, GPU2, 3)+b'\0',
                     addresses=(GPU1+8, GPU2+16), direct=False)
        cases.append({'name': name, 'status': result, 'events': m.events})

    print(json.dumps({'passed': True, 'hardware_access': False,
                      'runtime_sha256': RUNTIME_HASH, 'cudla_sha256': CUDLA_HASH,
                      'cases': cases,
                      'limitations': [
                          'Constructed objects and registries, modeled memory virtual methods and locks',
                          'Original builder, three helpers, lookup routines and memset execute',
                          'Five cases execute original runtime/cuDLA submission up to modeled D3DKMT calls',
                          'No OS submission, kernel translation or firmware execution in this test',
                          'Observed action bytes do not by themselves prove opcode semantics',
                          'No physical map/unmap or DMA-retirement guarantee']}, indent=2))


def outputs_unchanged(m):
    return (m.get(OUT) == m.get(OUT+8) == SENTINEL and
            bytes(m.u.mem_read(CPU+DESC, 64)) == b'\xa5'*64)


if __name__ == '__main__':
    main()
