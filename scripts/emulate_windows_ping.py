#!/usr/bin/env python3
"""Run original Windows ping and direct method wrapper against raw HAL-1 software.

OS allocation/cache/delay/callback edges are synthetic. RV32 reset/trap/DMA
functions execute unchanged against the explicit register model, not hardware.
"""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from emulate_profile1_commands import run as run_firmware, ROOT, HOST, PROFILE1_HASH

BASE = 0x140000000
CTX, DEVICE, OUTER, BUFFER, QUEUE, STACK, STUB = (0x32000000+i*0x10000 for i in range(7))
STOP = STUB+0xff0


def execute(pe, firmware, *, complete_after=0, allocation_error=0,
            null_cpu=False, dma=HOST, send_error=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd48000, 0xd49000, 0xd4a000, 0xd45000, 0xd4c000,
                 0xd38000, 0xd39000, 0xd3a000, 0xd3b000, 0xd57000, 0xcd000,
                 0xce000, 0xd8000, 0xe3e000, 0x1512000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, DEVICE, OUTER, BUFFER, QUEUE, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6')*5)
    for address, value in ((BASE+0x1512148, DEVICE), (BASE+0xe3eaf0, STUB),
                           (CTX+0xf8, BASE+0xd49fc0), (CTX+0x130, STUB+4),
                           (CTX+0x68, QUEUE)):
        u.mem_write(address, struct.pack('<Q', value))
    events = []
    delays = 0
    pending = None
    firmware_results = []

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def return_value(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        nonlocal delays, pending
        if address in (BASE+0xd39160, BASE+0xd38a38):
            return_value(OUTER)
        elif address == BASE+0xd3b0f8:
            assert reg(1) == DEVICE and reg(2) == 4096
            if not allocation_error:
                u.mem_write(reg(3), struct.pack('<Q', 0 if null_cpu else BUFFER))
                u.mem_write(reg(4), struct.pack('<Q', dma))
            events.append('allocate')
            return_value(allocation_error)
        elif address == BASE+0xcd148:
            assert reg(0) == BUFFER and reg(1) == 0 and reg(2) == 4096
            u.mem_write(BUFFER, bytes(4096))
            events.append('clear4096')
            return_value(BUFFER)
        elif address == BASE+0xd3b298:
            assert [reg(i) for i in (0, 1, 2)] == [OUTER, BUFFER, 4096]
            events.append(['cache', reg(3)])
            return_value(0)
        elif address == BASE+0xd3af88:
            return_value(1)  # Lock acquired: original direct-send branch.
        elif address in (BASE+0xd3afc8, BASE+0xd38758, BASE+0xd45180, BASE+0xce4b0):
            return_value(0)
        elif address == BASE+0xd4c800:
            assert [reg(i) for i in (0, 1, 2, 3)] == [DEVICE, QUEUE, dma >> 8, 1]
            assert struct.unpack('<I', u.mem_read(BUFFER, 4))[0] == 0x11111111
            events.append(['send_method', reg(3), reg(2)])
            if complete_after is not None and not send_error:
                assert dma == HOST
                result = run_firmware(firmware, method=1, boot_from_reset=True,
                                      via_trap=True, dma_register_model=True,
                                      host_payload=bytes(u.mem_read(BUFFER, 4096)))
                assert result['returned'] and result['firmware_code_unmodified']
                assert result['function_stubs'] == [] and not result['interrupts']
                assert result['host_word'] == 0x44444444
                pending = bytes.fromhex(result['host_payload_final'])
                assert pending == struct.pack('<I', 0x44444444)+bytes(4092)
                firmware_results.append({'method': 1, 'response_interrupts': 0,
                                         'dma_commands': len(result['dma_commands']),
                                         'trap_returns': result['trap_returns']})
                if complete_after == 0:
                    u.mem_write(BUFFER, pending)
                    pending = None
            return_value(send_error)
        elif address == BASE+0xd82c0:
            assert reg(0) == 100
            delays += 1
            if pending is not None and delays == complete_after:
                u.mem_write(BUFFER, pending)
                pending = None
            return_value(0)
        elif address == BASE+0xd3b328:
            assert [reg(i) for i in (0, 1, 2, 3)] == [OUTER, DEVICE, BUFFER, dma]
            events.append('free')
            return_value(0)
        elif address == STUB+4:
            events.append('final_callback')

    u.hook_add(UC_HOOK_CODE, hook)
    u.reg_write(regs.UC_ARM64_REG_X0, DEVICE)
    u.reg_write(regs.UC_ARM64_REG_X1, CTX)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0xd48110, STOP, count=200000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    return {'status': reg(0), 'events': events, 'delay_calls': delays,
            'firmware': firmware_results}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    firmware = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(firmware).hexdigest() == PROFILE1_HASH
    pe = pefile.PE(str(path), fast_load=True)
    rows = []
    for after in (0, 1, 17, 4999, 5000, None):
        result = execute(pe, firmware, complete_after=after)
        success = after is not None and after < 5000
        assert result['status'] == (0 if success else 0x28)
        assert result['delay_calls'] == (after if success else 5000)
        assert result['events'] == ['allocate', 'clear4096', ['cache', 1],
                                     ['send_method', 1, HOST >> 8], ['cache', 0],
                                     'free', 'final_callback']
        rows.append({'word_visible_after_delay_calls': after, **result})
    for args, expected in (({'allocation_error': 0x65}, 0x65),
                           ({'null_cpu': True}, 0x1a), ({'dma': 0}, 0x1f),
                           ({'send_error': 0x65}, 0x28)):
        result = execute(pe, firmware, **args)
        assert result['status'] == expected
        assert result['events'][-1] == 'final_callback'
        rows.append({'injected': args, **result})
    print(json.dumps({'passed': True, 'hardware_access': False, 'cases': rows,
                      'limitations': ['OS, allocator, cache and timing/lock boundaries are synthetic',
                                      'Only the explicitly selected direct-send wrapper branch is exercised',
                                      'Raw firmware code identity is not established for the opaque SROOT image',
                                      'Windows unconditional freeing and ignored sender error are observations, not safe Linux recovery policy',
                                      'No physical boot, IRQ routing, DMA coherency or inference is proven']}, indent=2))


if __name__ == '__main__':
    main()
