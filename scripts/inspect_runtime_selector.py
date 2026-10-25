#!/usr/bin/env python3
"""Execute original runtime selector construction/getters without naming its enum."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from inspect_diagnostic_contract import ROOT, BASE, RUNTIME_HASH, append

CTX, OUT, STACK, STOP = (0x3b000000+i*0x10000 for i in range(4))


def instance(pe):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xb000, 0x22000, 0x25000, 0x6b000):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, OUT, STACK, STOP):
        u.mem_map(address, 0x10000)
    return u


def call(u, entry, *args, end=STOP):
    for index, value in enumerate(args):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
    u.reg_write(regs.UC_ARM64_REG_LR, STOP)
    u.emu_start(BASE+entry, end, count=10000)
    assert u.reg_read(regs.UC_ARM64_REG_PC) == end
    return u.reg_read(regs.UC_ARM64_REG_X0)


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvdla_runtime.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == RUNTIME_HASH
    pe = pefile.PE(str(path), fast_load=True)
    # Prove which constructor vtable owns this particular getter slot; do not
    # substitute a same-offset field in a different runtime or KMD object.
    assert struct.unpack('<Q', pe.get_data(0x653d88+0x208, 8))[0] == BASE+0x255d0
    u = instance(pe)
    mutex_calls = []

    def hook(_, address, size, user):
        if address == BASE+0x6b270:
            assert u.reg_read(regs.UC_ARM64_REG_X0) == CTX+8
            mutex_calls.append(True)
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
            u.reg_write(regs.UC_ARM64_REG_PC, u.reg_read(regs.UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    call(u, 0xb910, CTX, end=BASE+0xb998)
    assert mutex_calls == [True]
    assert struct.unpack('<I', u.mem_read(CTX+0x98, 4))[0] == 0x7fffffff
    assert struct.unpack('<Q', u.mem_read(CTX, 8))[0] == BASE+0x653d88
    rows = []
    for selector in (0, 1, 2, 3, 42, 0x7fffffff, 0xffffffff):
        u = instance(pe)
        u.mem_write(CTX+0x98, struct.pack('<I', selector))
        direct = call(u, 0x22788, CTX)
        assert direct == (selector if selector <= 3 else 0x7fffffff)
        u.mem_write(OUT, b'\xa5'*8)
        status = call(u, 0x255d0, CTX, OUT)
        major = {0: 1, 1: 2, 3: 3}.get(selector)
        assert status == (0 if major is not None else 15)
        assert bytes(u.mem_read(OUT, 8)) == bytes([major or 0, 0, 0])+b'\xa5'*5
        diagnostic = append(pe, selector)
        assert diagnostic['status'] == (0 if selector == 1 else 2)
        rows.append({'selector': selector, 'direct_getter': direct,
                     'three_byte_getter_status': status, 'three_byte_getter_output': [major or 0, 0, 0],
                     'diagnostic_append_status': diagnostic['status']})
    print(json.dumps({'passed': True, 'hardware_access': False, 'constructor': '0xb910',
                      'constructor_prefix_end': '0xb998', 'constructor_selector_default': '0x7fffffff',
                      'constructor_vtable': '0x653d88', 'three_byte_getter_slot': '0x208',
                      'direct_getter': '0x22788', 'three_byte_getter': '0x255d0', 'cases': rows,
                      'limitations': ['Only constructor prefix executes; mutex and diagnostic model-parser boundaries are modeled',
                                      'Getter bytes are observed without assigning an enum/version/hardware meaning',
                                      'No selector-setting path, live Windows value or KMD classifier correspondence is established',
                                      'No device initialization, physical firmware compatibility, diagnostic completion or inference']}, indent=2))


if __name__ == '__main__':
    main()
