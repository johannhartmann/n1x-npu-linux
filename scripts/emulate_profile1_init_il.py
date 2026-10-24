#!/usr/bin/env python3
"""Run diagnostic INIT_IL through actual P16 RV32 table/opcode handlers.

Engine registers are RAM only. Register values are synthetic readiness inputs;
no NPU state or hardware register is accessed.
"""
import hashlib
import json
import struct
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_HOOK_CODE, UC_HOOK_MEM_READ
from unicorn import riscv_const as regs
from emulate_firmware_commands import ROOT
from extract_profile1_firmware import PROFILE1_HASH
from inspect_loadable import Buffer

ENGINE, CACHE, STACK, STOP = 0x01400000, 0x184400, 0x20100000, 0x20200000


def run(blob, instructions, count, ready=True, slice_data=None, ib_data=None, network_data=None, network_capacity=256):
    u = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
    for address, size in ((0x100000, 0x9000), (0x180000, 0x10000),
                          (ENGINE, 0x10000), (STACK, 0x10000), (STOP, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(0x100000, blob[512:512+36864])
    u.mem_write(0x180000, blob[512+36864:])
    u.mem_write(0x101c96, bytes.fromhex('67800000'))
    reads, handlers, dma = [], [], []
    host, context, address_list = 0x20400000, 0x20300000, 0x2041f000
    if slice_data is not None:
        assert ib_data is not None and len(slice_data) == 12 and len(ib_data) <= 0x1f000
        u.mem_map(host, 0x20000)
        u.mem_map(context, 0x2000)
        u.mem_write(host, ib_data)
        u.mem_write(context, struct.pack('<I', context+0x1000))
        u.mem_write(context+0x1000, struct.pack('<QH', address_list, 2))
        if network_data is not None:
            assert len(network_data) == 128
            u.mem_write(host+0x1e900, network_data+bytes(128))
        u.mem_write(address_list, struct.pack('<QQ', host+0x1e900, host))
        u.mem_write(STACK+0x1000, slice_data)
        u.mem_write(0x104ed8, bytes.fromhex('67800000'))

    def code(_, address, size, user):
        if address == 0x101c96:
            u.reg_write(regs.UC_RISCV_REG_A0, 0)
        elif address == 0x102a92:
            handlers.append(hex(address))
        elif address == 0x104ed8:
            a = [u.reg_read(getattr(regs, f'UC_RISCV_REG_A{i}')) for i in range(4)]
            source, destination, length = a[0] | a[1] << 32, a[2], a[3]
            in_list = address_list <= source and source+length <= address_list+16
            in_ib = host <= source and source+length <= host+len(ib_data)
            in_network = network_data is not None and host+0x1e900 <= source and source+length <= host+0x1e900+network_capacity
            assert in_list or in_ib or in_network, 'DMA exceeds explicit host allocation'
            dma.append({'source': hex(source), 'destination': hex(destination), 'bytes': length})
            u.mem_write(destination, bytes(u.mem_read(source, length)))
            u.reg_write(regs.UC_RISCV_REG_A0, 0)

    def read(_, access, address, size, value, user):
        if ENGINE <= address < ENGINE+0x10000:
            reads.append({'offset': hex(address-ENGINE), 'bytes': size})
            # Hook runs before the actual RAM read.
            u.mem_write(address, (1 if ready else 0).to_bytes(size, 'little'))

    u.hook_add(UC_HOOK_CODE, code)
    u.hook_add(UC_HOOK_MEM_READ, read)
    u.reg_write(regs.UC_RISCV_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(0x102cec, STOP, count=1000)
    assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
    table = {str(index): hex(struct.unpack('<I', u.mem_read(0x185d6c+index*4, 4))[0])
             for index in range(256) if struct.unpack('<I', u.mem_read(0x185d6c+index*4, 4))[0]}
    if slice_data is None:
        u.mem_write(CACHE, bytes(instructions))
        u.reg_write(regs.UC_RISCV_REG_A0, STACK+0x1000)
        u.reg_write(regs.UC_RISCV_REG_A1, count)
        entry = 0x102abe
    else:
        u.reg_write(regs.UC_RISCV_REG_A0, context)
        u.reg_write(regs.UC_RISCV_REG_RA, STOP)
        u.emu_start(0x104a92, STOP, count=10000)
        assert u.reg_read(regs.UC_RISCV_REG_PC) == STOP
        assert u.reg_read(regs.UC_RISCV_REG_A0) == 0
        u.reg_write(regs.UC_RISCV_REG_A0, context)
        u.reg_write(regs.UC_RISCV_REG_A1, STACK+0x1000)
        entry = 0x102d7a
    end = STOP
    if network_data is not None:
        # Original v2 prefix resolves address-list[0], reads 256 network bytes,
        # installs handlers, then calls the real init slice at network +20.
        u.reg_write(regs.UC_RISCV_REG_S0, context)
        u.reg_write(regs.UC_RISCV_REG_S1, 0x185740)
        entry, end = 0x100fea, 0x101052
    u.reg_write(regs.UC_RISCV_REG_RA, STOP)
    u.emu_start(entry, end, count=2000)
    status = u.reg_read(regs.UC_RISCV_REG_A0)
    return {'returned': u.reg_read(regs.UC_RISCV_REG_PC) == end,
            'pc': hex(u.reg_read(regs.UC_RISCV_REG_PC)),
            'status': status-(1 << 32) if status >= 1 << 31 else status,
            'register_reads': reads, 'handlers': handlers, 'installed_handlers': table, 'dma': dma}


def main():
    blob = (ROOT/'sources/dla-profile1-verified.bin').read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROFILE1_HASH
    data = (ROOT/'sources/diagnostic-verified.nvda').read_bytes()
    assert hashlib.sha256(data).hexdigest() == '51d26d83317807984d6a4b29be68e6e370175a0a0bd30fad2179a057e3c28543'
    b = Buffer(data)
    blobs = {}
    for table in b.tables(b.u('<I', 0), 5):
        vector = b.vector(table, 5, 1)
        blobs[b.string(table, 0)] = data[vector.start:vector.stop]
    ib = blobs['task0-ib']
    slice_data = blobs['task0-addr0'][20:32]
    assert struct.unpack('<IIHH', slice_data) == (1, 122880, 3, 16)
    instructions = ib[122880:122928]
    success = run(blob, instructions, 3)
    assert success['returned'] and success['status'] == 0
    assert success['register_reads'] == [{'offset': hex(x), 'bytes': 4} for x in (0x7008, 0x5000, 0x6000)]
    assert success['handlers'] == ['0x102a92']*3
    chained = run(blob, instructions, 3, slice_data=slice_data, ib_data=ib)
    assert chained['returned'] and chained['status'] == 0
    assert chained['register_reads'] == success['register_reads']
    assert chained['handlers'] == success['handlers']
    assert [item['bytes'] for item in chained['dma']] == [16, 256]
    assert chained['dma'][1]['source'] == hex(0x20400000+122880)
    network_chain = run(blob, instructions, 3, slice_data=slice_data, ib_data=ib,
                        network_data=blobs['task0-addr0'])
    assert network_chain['returned'] and network_chain['status'] == 0
    assert network_chain['register_reads'] == success['register_reads']
    assert network_chain['handlers'] == success['handlers']
    assert [item['bytes'] for item in network_chain['dma']] == [16, 256, 256]
    assert network_chain['dma'][1]['source'] == hex(0x2041e900)
    try:
        run(blob, instructions, 3, slice_data=slice_data, ib_data=ib,
            network_data=blobs['task0-addr0'], network_capacity=128)
    except AssertionError as error:
        assert str(error) == 'DMA exceeds explicit host allocation'
    else:
        raise AssertionError('128-byte network backing unexpectedly accepted')
    waiting = run(blob, instructions, 3, ready=False)
    assert not waiting['returned'] and len(waiting['handlers']) == 1
    assert len(waiting['register_reads']) > 10
    assert all(item['offset'] == '0x7008' for item in waiting['register_reads'])
    bad_subtype = bytearray(instructions[:16])
    bad_subtype[1] = 0
    rejected = run(blob, bad_subtype, 1)
    assert rejected['returned'] and rejected['status'] == -3 and not rejected['register_reads']
    too_many = run(blob, instructions, 17)
    assert too_many['returned'] and too_many['status'] == -3 and not too_many['register_reads']
    unknown = run(blob, bytes(16), 1)
    assert unknown['returned'] and unknown['status'] == -3 and not unknown['register_reads']
    print(json.dumps({'passed': True, 'hardware_access': False, 'firmware_sha256': PROFILE1_HASH,
                      'scope': 'P16 RV32 table initialization, IL batch dispatcher and poll handler with RAM registers',
                      'architecture': 'RV32', 'batch_limit_case': too_many, 'loadable_slice_chain': chained, 'network_prefix_chain': network_chain, 'undersized_network_backing_detected': True,
                      'engine_internal_base': hex(ENGINE), 'instruction_hex': instructions.hex(),
                      'rejected_subtype_case': rejected, 'ready_case': success, 'not_ready_case': waiting, 'unknown_opcode_case': unknown,
                      'limitations': ['Ready bits are synthetic, not observed NPU status',
                                      'Network/IL DMA is a checked RAM copy; execution stops before complete task scheduling',
                                      'Observation budget exhaustion is not a firmware timeout',
                                      'Internal firmware addresses do not identify host MMIO mappings']}, indent=2))


if __name__ == '__main__':
    main()
