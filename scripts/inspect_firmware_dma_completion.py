#!/usr/bin/env python3
"""Execute original HAL-1 DMA wrappers with delayed, bounded RAM-only transfers."""
import hashlib
import json
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as r

from emulate_profile1_boot import boot
from emulate_profile1_commands import ROOT, HOST, MMIO, PROFILE1_HASH


def check(blob, direction, delay, *, stuck=False, stall_at=1, busy_status=1,
          status_error=0, bus_error=0):
    u, state = boot(blob)
    assert state['scheduler_wfi']
    stop, local, size = 0x40000000, 0x188000, 516
    u.mem_map(HOST & ~4095, 8192)
    u.mem_map(stop, 4096)
    payload = bytes((i*17+3) & 255 for i in range(size))
    source, destination = (HOST, local) if direction == 'read' else (local, HOST)
    u.mem_write(source, payload)
    u.mem_write(destination, b'\xa5'*size)
    commands, reads = [], []
    pending = None

    def word(offset):
        return int.from_bytes(u.mem_read(MMIO+offset, 4), 'little')

    def write(_, access, address, width, value, user):
        nonlocal pending
        if address != MMIO+0x118:
            return
        assert width == 4 and value & ~0x720 == 0x2000
        assert pending is None, 'Original firmware issued over an unfinished transfer'
        length = 4 << ((value >> 8) & 7)
        external = (word(0x128) << 40)+(word(0x110) << 8)+word(0x11c)
        internal = 0x180000+word(0x114)
        assert HOST <= external and external+length <= HOST+size
        assert local <= internal and internal+length <= local+size
        assert bool(value & 0x20) == (direction == 'write')
        src, dst = (internal, external) if value & 0x20 else (external, internal)
        pending = {'source': src, 'destination': dst, 'bytes': length,
                   'polls': 0, 'completed': False}
        commands.append(pending)

    def read(_, access, address, width, value, user):
        nonlocal pending
        if address == MMIO+0x2e0:
            u.mem_write(address, struct.pack('<I', bus_error if commands else 0))
        if address != MMIO+0x118:
            return
        status = 2
        if pending is not None:
            pending['polls'] += 1
            if (stuck and len(commands) == stall_at) or pending['polls'] <= delay:
                status = busy_status  # bit 0 blocks issue; clear bit 1 blocks completion.
            else:
                src, dst, length = (pending[k] for k in ('source', 'destination', 'bytes'))
                u.mem_write(dst, bytes(u.mem_read(src, length)))
                pending['completed'] = True
                pending = None
        # Inject the completion fault only after some work was actually issued.
        if commands and status == 2:
            status |= status_error
        reads.append({'pc': hex(u.reg_read(r.UC_RISCV_REG_PC)), 'status': status})
        u.mem_write(address, struct.pack('<I', status))

    u.hook_add(UC_HOOK_MEM_READ, read)
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    args = (HOST, 0, local, size, 0) if direction == 'read' else (local, HOST, 0, size, 0)
    for i, value in enumerate(args):
        u.reg_write(getattr(r, f'UC_RISCV_REG_A{i}'), value)
    u.reg_write(r.UC_RISCV_REG_RA, stop)
    u.emu_start(0x104ed8 if direction == 'read' else 0x105004, stop, count=30000)
    pc, status = u.reg_read(r.UC_RISCV_REG_PC), u.reg_read(r.UC_RISCV_REG_A0)
    status = status if status < 1 << 31 else status-(1 << 32)
    output = bytes(u.mem_read(destination, size))
    assert bytes(u.mem_read(0x100000, 36864)) == blob[512:512+36864]
    if stuck:
        assert pc != stop and pending is not None
        assert len(commands) == stall_at
        completed = sum(c['bytes'] for c in commands if c['completed'])
        assert output == payload[:completed]+b'\xa5'*(size-completed)
        assert pc in ((0x100128, 0x10012c, 0x100130) if stall_at == 1
                      else (0x100266, 0x10026a, 0x10026e))
    elif status_error or bus_error:
        assert pc == stop and status == (-18 if direction == 'read' else -4) and pending is None
        assert output == payload and sum(c['bytes'] for c in commands) == size
    else:
        assert pc == stop and status == 0 and pending is None and output == payload
        assert sum(c['bytes'] for c in commands) == size
        assert all(c['completed'] and c['polls'] == delay+1 for c in commands)
    return {'direction': direction, 'delay_reads': delay, 'stuck': stuck,
            'stall_at': stall_at if stuck else None, 'busy_status': busy_status,
            'status_error': hex(status_error), 'bus_error': hex(bus_error),
            'returned': pc == stop, 'return_status': status if pc == stop else None,
            'pc': hex(pc), 'commands': commands,
            'status_read_count': len(reads),
            'poll_pcs': sorted(set(v['pc'] for v in reads)),
            'output_matches': output == payload}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    cases = []
    for direction in ('read', 'write'):
        for delay in (0, 1, 17):
            cases.append(check(blob, direction, delay))
        cases.append(check(blob, direction, 0, stuck=True))
        for busy in (0, 1):
            cases.append(check(blob, direction, 0, stuck=True, stall_at=3, busy_status=busy))
        cases.append(check(blob, direction, 1, status_error=1 << 20))
        cases.append(check(blob, direction, 1, bus_error=4))
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'firmware_sha256': PROFILE1_HASH, 'cases': cases,
                      'limitations': ['RAM-only DMA with explicitly synthetic status transitions',
                                      'No arithmetic-engine transfers or real bus ordering modeled',
                                      'Instruction-budget exhaustion is not a firmware timeout',
                                      'Does not establish safe physical DMA reclamation']}, indent=2))


if __name__ == '__main__':
    main()
