#!/usr/bin/env python3
"""Execute original OSROOT reply code against synthetic registers, never hardware."""
import hashlib
import json
from pathlib import Path
import struct

import pefile
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn import riscv_const as regs

from test_sroot_receive import windows
from test_sroot_send import HASH as PROVIDER_HASH

ROOT = Path(__file__).resolve().parents[1]
CODE_HASH = '08adaedac470ec367e74a644663ab68b92023fc47178aaec896f2a1ae468eb47'
DATA_HASH = '1086a49e17844e2aad6990a70f1c25a979ecf08f09991e8ce7f203da8791b7d8'
CODE = 0x15000
DATA, COOKIE, STACK, PACKET, MMIO, STOP = (
    0x144000, 0x17d000, 0x300000, 0x310000, 0x320000, 0x330000)


def execute(code, data, mailbox, status, command, words, token=0x42, null=False,
            service_result=None):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    # Text base follows the startup cookie reference and scrub boundaries.
    # The full boot environment, peripherals and authentication remain unmodeled.
    for address, size in ((CODE, 0x88000), (DATA, 0x3a000),
                          (STACK, 0x10000), (PACKET, 0x1000),
                          (MMIO, 0x4000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(CODE, code)
    u.mem_write(DATA, data)
    # Original accessor 0x31c0 reads this four-entry register-base array.
    u.mem_write(0x146d44, struct.pack('<4I', *(MMIO + i*0x1000 for i in range(4))))
    u.mem_write(COOKIE+0x6c, struct.pack('<I', 0x12345678))
    packet = struct.pack('<III', status, command, len(words))
    packet += struct.pack('<'+'I'*len(words), *words)
    u.mem_write(PACKET, packet)
    trace = []

    def write(_, access, address, size, value, user):
        assert size == 4
        trace.append((address-MMIO, value))

    u.hook_add(UC_HOOK_MEM_WRITE, write, begin=MMIO, end=MMIO+0x3fff)
    injected_calls = []
    if service_result is not None:
        def external(_, address, size, user):
            offset = address-CODE
            if offset not in (0x6088, 0x5004):
                return
            if offset == 0x6088:
                injected_calls.append(offset)
                u.reg_write(regs.UC_RISCV_REG_A0, service_result)
            # Logging is suppressed; the auth operation returns an injected
            # result. Error-ring bookkeeping and reply construction run intact.
            u.reg_write(regs.UC_RISCV_REG_PC, u.reg_read(regs.UC_RISCV_REG_RA))
        u.hook_add(UC_HOOK_CODE, external)
        u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.reg_write(regs.UC_RISCV_REG_A0, PACKET)
        u.reg_write(regs.UC_RISCV_REG_A1, PACKET+0x100)
        u.emu_start(CODE+0x109f4, STOP, count=10000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        assert injected_calls == [0x6088]
        packet = bytes(u.mem_read(PACKET+0x100, 12))
        u.mem_write(PACKET, packet)
    for register, value in ((regs.UC_RISCV_REG_SP, STACK+0x8000),
                            (regs.UC_RISCV_REG_RA, STOP),
                            (regs.UC_RISCV_REG_A0, mailbox),
                            (regs.UC_RISCV_REG_A1, 0 if null else PACKET),
                            (regs.UC_RISCV_REG_A2, token)):
        u.reg_write(register, value)
    u.emu_start(CODE+0x341c, STOP, count=10000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    assert bytes(u.mem_read(PACKET, len(packet))) == packet
    return u.reg_read(regs.UC_RISCV_REG_A0), trace


def main():
    directory = ROOT/'sources/dgx-spark-osroot'
    code = (directory/'osroot-2100.bin').read_bytes()
    data = (directory/'osroot-2200.bin').read_bytes()
    assert hashlib.sha256(code).hexdigest() == CODE_HASH
    assert hashlib.sha256(data).hexdigest() == DATA_HASH
    cases = 0
    for mailbox in range(4):
        for count in (0, 1, 3, 14, 15):
            for status in (0, 0x2a006, 0x253ff, 0xffffffff):
                words = [(i*0x87654321) & 0xffffffff for i in range(count)]
                command, token = 0x18, 0xa5
                header = ((status & 0xfff) << 12 | ((status >> 12) & 255) << 24
                          | count << 8 | command)
                base = mailbox*0x1000
                expected = [(base+0x210, token), (base+0x214, header)]
                expected += [(base+0x214, word) for word in words]
                expected += [(base+0x21c, 1)]
                assert execute(code, data, mailbox, status, command, words, token) == (0, expected)
                cases += 1
    for mailbox, null in ((4, False), (0xffffffff, False), (0, True)):
        assert execute(code, data, mailbox, 0, 0x18, [], null=null) == (1, [])
    provider = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(provider.read_bytes()).hexdigest() == PROVIDER_HASH
    pe = pefile.PE(str(provider), fast_load=True)
    live = []
    for status, expected_header in ((0x2a006, 0x2a006018), (0x253ff, 0x253ff018)):
        result, trace = execute(code, data, 0, status, 0x18, [])
        header = trace[1][1]
        assert result == 0 and header == expected_header
        _, decoded = windows(pe, 16, header, [])
        assert decoded == (0x18, status & 0xfff, (status >> 12) & 255, 0, [])
        live.append({'header': hex(header), 'decoded': decoded})
    # The real command-0x19 service translates an injected inner return value.
    # These are counterfactual inputs, not observed P16 internal errors.
    service_cases = []
    for raw, combined in ((0x55aa0000, 0), (0xaa55a806, 0x2a006),
                          (0xaa5597ff, 0x253ff), (0, 0), (0xffffffff, 0x3f3ff)):
        result, trace = execute(code, data, 0, 0, 0x19, [4096, 0x12340000],
                                service_result=raw)
        expected = 0x19 | (combined & 0xfff) << 12 | ((combined >> 12) & 255) << 24
        assert result == 0 and trace == [(0x210, 0x42), (0x214, expected), (0x21c, 1)]
        service_cases.append({'injected_internal_return': hex(raw),
                              'combined_status': hex(combined), 'header': hex(expected)})
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'original_function_offsets': ['0x341c', '0x31c0', '0x36b6'],
                      'reply_cases': cases, 'invalid_argument_cases': 3,
                      'windows_decoder_roundtrips': live,
                      'text_base': hex(CODE),
                      'service_19_injected_result_cases': service_cases,
                      'limitations': [
                          'Synthetic register bases, packet, stack and cookie',
                          'Reconstructed text/data bases do not model full boot or authentication',
                          'Service 0x19 uses an injected auth return and suppressed logging',
                          'No claim that command 0x18 uses the same internal error conversion',
                          'OSROOT is a reference image, not identified P16 SROOT firmware',
                          'Packing equivalence does not establish error meanings or hardware acceptance',
                          'Count 15 works here; the Linux transport deliberately accepts at most 14'
                      ]}, indent=2))


if __name__ == '__main__':
    main()
