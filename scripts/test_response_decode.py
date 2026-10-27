#!/usr/bin/env python3
"""Compare C mailbox field decoding with the original Windows HAL-1 parser."""
import ctypes
import errno
import hashlib
import json
import os
import shlex
import struct
import subprocess
import tempfile
from pathlib import Path
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install
from extract_firmware import KMD_SHA256
from test_response import Response, ResponseFields as Fields

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, VTABLE, OUT, STACK, STUB = (0x29000000+i*0x10000 for i in range(5))
STOP = STUB+0xff0


def windows(pe, word, data=0x123456, fail_at=None, irq=None, outstanding=2, idle_enabled=0):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for rva, size in ((0x1951000, 0xd000), (0xd48000, 0x3000), (0xd3c000, 0x1000), (0xd47000, 0x1000), (0xd43000, 0x1000)):
        u.mem_map(BASE+rva, size)
        u.mem_write(BASE+rva, pe.get_data(rva, size))
    for address, size in ((BASE+0xd57000, 0x1000), (BASE+0xd39000, 0x1000),
                          (BASE+0xe3e000, 0x1000), (BASE+0xce000, 0x1000),
                          (BASE+0xd38000, 0x1000), (BASE+0xd3a000, 0x1000),
                          (BASE+0xd3b000, 0x1000), (BASE+0x1512000, 0x1000),
                          (BASE+0x13ac000, 0x1000), (BASE+0xcd000, 0x1000),
                          (CTX, 0x10000), (VTABLE, 0x10000), (OUT, 0x10000),
                          (STACK, 0x10000), (STUB, 0x10000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for address in (STUB, BASE+0xd39878, BASE+0xce4b0):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    u.mem_write(CTX+0x496, struct.pack('<H', 1))
    u.reg_write(regs.UC_ARM64_REG_X0, CTX)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+0x1951238, STOP, count=50000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    u.mem_write(CTX+0x47d, bytes((idle_enabled,)))
    u.mem_write(CTX+0x68, struct.pack('<Q', CTX+8))
    u.mem_write(CTX+8, struct.pack('<Q', VTABLE))
    u.mem_write(VTABLE+0xd0, pe.get_data(0x108e9b8+0xd0, 8))
    u.mem_write(OUT, struct.pack('<III', *([0xa5a5a5a5]*3)))
    trace, writes, dumps, reports, events = [], [], [], [], []
    if irq is not None:
        for address in (STUB+0x20, BASE+0xd39f38, BASE+0xd39160, BASE+0xd38a38,
                        BASE+0xd3af88, BASE+0xd3afc8, BASE+0xd3b548, BASE+0xd38758, BASE+0xcd148):
            u.mem_write(address, ret)
        u.mem_write(CTX+0x418, struct.pack('<Q', STUB+0x20))
        u.mem_write(CTX+0x5b4, struct.pack('<I', outstanding))
        u.mem_write(VTABLE+0xd8, pe.get_data(0x108e9b8+0xd8, 8))

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def hook(_, address, size, user):
        if address == BASE+0xd39878:
            assert reg(1) == 0
            assert irq is not None or reg(2) in (0x40, 0x44)
            trace.append(reg(2))
            u.mem_write(reg(3), struct.pack('<I', word if reg(2) == 0x40 else data if reg(2) == 0x44 else 0))
            u.reg_write(regs.UC_ARM64_REG_X0, 0x65 if len(trace)-1 == fail_at else 0)
        elif irq is not None and address == BASE+0xcd148:
            u.mem_write(reg(0), bytes([reg(1) & 255])*reg(2))
        elif irq is not None and address == BASE+0xd39f38:
            writes.append((reg(2), reg(3)))
            events.append('ack')
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif irq is not None and address == STUB+0x20:
            dumps.append(reg(3))
            events.append('dump')
            assert bytes(u.mem_read(CTX+0x5ac, 1)) == b'\x01'
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif irq is not None and address == BASE+0xd3b548:
            reports.append(reg(7))
            events.append('report')
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif irq is not None and address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, OUT+0x1000)
        elif irq is not None and address == BASE+0xd3af88:
            u.reg_write(regs.UC_ARM64_REG_X0, 1)
        elif irq is not None and address in (BASE+0xd38a38, BASE+0xd3afc8, BASE+0xd38758):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == BASE+0xce4b0:
            u.reg_write(regs.UC_ARM64_REG_X0, 0)

    install(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for i, value in enumerate((0x12340000, CTX, OUT, OUT+4, OUT+8)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    if irq is not None:
        for i, value in enumerate((OUT+0x1000, CTX, irq)):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
    try:
        u.emu_start(BASE+(0xd48ce0 if irq is not None else 0xd48910), STOP, count=10000)
    except Exception as exc:
        raise RuntimeError(f'PC={u.reg_read(regs.UC_ARM64_REG_PC):x}') from exc
    assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
    if irq is not None:
        return {'status': reg(0), 'reads': trace, 'writes': writes, 'dumps': dumps, 'reports': reports, 'events': events,
                'outstanding': struct.unpack('<I', u.mem_read(CTX+0x5b4, 4))[0],
                'state_5ac': bytes(u.mem_read(CTX+0x5ac, 1))[0]}
    command, message, status = struct.unpack('<III', u.mem_read(OUT, 12))
    return reg(0), (message, command, status), trace


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    with tempfile.TemporaryDirectory(prefix='n1x-response-decode-') as tmp:
        library = Path(tmp)/'response.so'
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_dla_response.c'),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    decode = lib.n1x_dla_decode_response
    decode.argtypes = [ctypes.POINTER(Response), ctypes.POINTER(Fields)]
    words = {0, 0xffffffff, 0x102, 0x40101, 0x110101, 0x702, 0x90701, 0x8003070a}
    for shift in (0, 8, 16, 24):
        words.update(value << shift for value in range(256))
    for word in sorted(words):
        response, output = Response(word, 0x123456), Fields()
        assert decode(ctypes.byref(response), ctypes.byref(output)) == 0
        code, actual, reads = windows(pe, word)
        assert code == 0 and reads == [0x40, 0x44]
        assert actual == (output.message, output.command, output.status)
        assert output.upper == word >> 24 and output.data == response.data
    for fail_at in (0, 1):
        status, fields, reads = windows(pe, 0x8003070a, fail_at=fail_at)
        assert status == 0x65 and fields == (0xa5a5a5a5,)*3
        assert reads == [0x40, 0x44][:fail_at+1]
    output = Fields(1, 2, 3, 4, 5)
    assert decode(None, ctypes.byref(output)) == -errno.EINVAL
    assert (output.message, output.command, output.status, output.upper, output.data) == (1, 2, 3, 4, 5)
    assert decode(ctypes.byref(Response()), None) == -errno.EINVAL
    irq_cases = []
    for message in (2, 8, 9, 10):
        for status_byte in (0, 3, 15, 255):
            word = (status_byte << 16) | (7 << 8) | message
            result = windows(pe, word, irq=0x80)
            report = (0x98 if message == 9 else
                      0x99 if message == 8 or status_byte == 15 else
                      0x8a if message == 8 or status_byte not in (0, 255) else None)
            assert result['status'] == 0 and result['writes'] == [(0x40, 0xd1a0cafe)]
            assert result['events'] == (['ack'] if report is None else ['dump', 'ack', 'report'])
            assert result['outstanding'] == 2
            assert result['state_5ac'] == int(report is not None)
            assert result['dumps'] == result['reports'] == ([] if report is None else [report])
            assert result['reads'] == ([0x40, 0x44] if report is None else
                                       [0x40, 0x44, 0x338, 0x33c, 0x90, 0x94])
            irq_cases.append({'word': hex(word), 'report_code': report, 'state_5ac': result['state_5ac']})
    for word in (0x8003070a, 0x90701):
        result = windows(pe, word, irq=0x80)
        assert result['dumps'] == result['reports'] == [0x8a] and result['state_5ac'] == 1
        assert result['writes'] == [(0x40, 0xd1a0cafe)] and result['status'] == 0
        irq_cases.append({'word': hex(word), 'report_code': 0x8a, 'state_5ac': 1})
    for initial in (0, 1, 2):
        result = windows(pe, 0, irq=0x40, outstanding=initial)
        assert result == {'status': 0, 'reads': [], 'writes': [], 'dumps': [], 'reports': [], 'events': [],
                          'outstanding': max(0, initial-1), 'state_5ac': 0}
    idle_cases = 0
    # Actual board +0x47d=1 as well as disabled policy. The synthetic host
    # context is ineligible for power-down; no OS power transition is emulated.
    for idle_enabled in (0, 1):
        for irq in (0x80, 0x81, 0xc1):
            for initial in (0, 2):
                result = windows(pe, 12, data=0, irq=irq, outstanding=initial,
                                 idle_enabled=idle_enabled)
                assert result == {'status': 0, 'reads': [0x40, 0x44],
                                  'writes': [(0x40, 0xd1a0cafe)], 'dumps': [],
                                  'reports': [], 'events': ['ack'],
                                  'outstanding': max(0, initial-int(bool(irq & 0x40))),
                                  'state_5ac': 0}
                idle_cases += 1
    combined = windows(pe, 0x702, irq=0xc0)
    assert combined['outstanding'] == 1 and combined['writes'] == [(0x40, 0xd1a0cafe)]
    assert not combined['dumps'] and combined['state_5ac'] == 0
    print(json.dumps({'passed': True, 'hardware_access': False, 'original_parser': '0xd48910',
                      'differential_words': len(words), 'reader_error_cases': 2,
                      'original_irq_handler': '0xd48ce0', 'irq_response_cases': irq_cases,
                      'idle_notification_cases': idle_cases, 'swgen0_counter_cases': 3, 'combined_swgen0_swgen1_checked': True,
                      'limitations': ['Mailbox reads are synthetic; HAL initializer, parser and accessors execute',
                                      'Upper byte and data are preserved without assigning semantics',
                                      'IRQ lock, diagnostic dump and report boundaries are stubbed; no recovery executes',
                                      'Idle tests retain a power-down-ineligible synthetic host context',
                                      'Decoding alone does not establish task completion or DMA quiescence']}, indent=2))


if __name__ == '__main__':
    main()
