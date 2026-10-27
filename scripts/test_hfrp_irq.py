#!/usr/bin/env python3
"""Cross-check original Windows HFRP IRQ, parent dispatch, L4T header and C port."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_cookie
from extract_firmware import KMD_SHA256
from test_core_control import IO, READ, WRITE, DELAY

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x140000000
CTX, LINK, OWNER, PARENT, ROOTCTX, PRIMARY, OUTER, STACK, STUB = (0x34000000+i*0x10000 for i in range(9))
STOP = STUB+0xff0
HEADER_HASH = 'ca7bc4d365772cedcc09ae08b43cd2f232a581b14c748d9b8642e33b2e3f6e89'


class Result(ctypes.Structure):
    _fields_ = [('status', ctypes.c_uint32), ('status_valid', ctypes.c_uint8),
                ('ack_attempted', ctypes.c_uint8), ('acknowledged', ctypes.c_uint8)]


def windows(pe, status, *, mode=0, counter=0, pending=0, read_error=0,
            write_error=0, read_output=True, parent=False, primary_status=0,
            enabled=True, primary_error=0, original_primary=False, owned=0xc0,
            entry_wrapper=False, entry_input=0, global_present=True,
            original_dpc=False, dpc_locks=(True,), outstanding=2,
            queue_already_pending=False, primary_disabled=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page in (0xd38000, 0xd39000, 0xd3d000, 0xd4a000, 0xd4b000,
                 0xd57000, 0xcd000, 0xe3e000, 0x238000, 0xd48000,
                 0xd45000, 0xd36000, 0xd3b000, 0x93000, 0xce000,
                 0xd43000, 0xd44000, 0xd49000, 0xd3a000,
                 0x108e000, 0x142000, 0x641000, 0x13ac000, 0xed000,
                 0x1512000, *range(0x1951000, 0x195e000, 0x1000)):
        u.mem_map(BASE+page, 0x1000)
        u.mem_write(BASE+page, pe.get_data(page, 0x1000))
    for address in (CTX, LINK, OWNER, PARENT, ROOTCTX, PRIMARY, OUTER, STACK, STUB):
        u.mem_map(address, 0x10000)
    install_cookie(u, pe)
    u.mem_write(STUB, bytes.fromhex('c0035fd6')*16)
    u.mem_write(BASE+0xe3eaf0, struct.pack('<Q', STUB))
    trace, primary_calls, schedules, notifications = [], [], [], []
    dpc_events, dpc_registration, queue, lock_results = [], [], [], list(dpc_locks)
    dpc_object = OUTER+0x4000

    def reg(i):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))

    def ret(value):
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, reg(30))

    def hook(_, address, size, user):
        if address == BASE+0xcd148:
            u.mem_write(reg(0), bytes(reg(2)))
            ret(reg(0))
        elif address == BASE+0xd39878:
            if original_primary and reg(1) == 0:
                assert [reg(i) for i in (0, 1, 2)] == [ROOTCTX, 0, 8]
                primary_calls.append(primary_status)
                u.mem_write(reg(3), struct.pack('<I', primary_status))
                ret(primary_error)
            else:
                assert [reg(i) for i in (0, 1, 2)] == [OWNER, 2, 0x104]
                trace.append(('read', 0x104, status))
                if read_output:
                    u.mem_write(reg(3), struct.pack('<I', status))
                ret(read_error)
        elif address == BASE+0xd39f38:
            if original_dpc and reg(1) == 0:
                assert [reg(i) for i in (0, 1, 2)] == [ROOTCTX, 0, 4]
                dpc_events.append(['irq_clear', reg(3)])
                ret(write_error)
                return
            assert [reg(i) for i in (0, 1, 2)] == [OWNER, 2, 0x10c]
            trace.append(('write', 0x10c, reg(3)))
            ret(write_error)
        elif address == BASE+0xd38a38:
            ret(OUTER)
        elif address == BASE+0x2385c0:
            ret(0x12345678)
        elif address == BASE+0xd38758:
            ret(0)
        elif address == STUB+4:
            assert reg(1) == PRIMARY
            primary_calls.append(primary_status)
            u.mem_write(reg(2), struct.pack('<I', primary_status))
            ret(primary_error)
        elif address == STUB+8:
            schedules.append(True)
            ret(0)
        elif address == STUB+12:
            assert original_primary and reg(0) == ROOTCTX and reg(1) == OUTER+0x1000
            schedules.append(struct.unpack('<I', u.mem_read(reg(2), 4))[0])
            ret(0)
        elif address == BASE+0x93630:
            assert reg(0) == OWNER
            ret(ROOTCTX)
        elif address == BASE+0xd39160:
            assert reg(0) == OWNER
            ret(OUTER+0x3000)
        elif address == BASE+0xd3b4a0:
            assert reg(0) == OUTER+0x3000
            notifications.append(reg(1))
            if not original_dpc:
                assert reg(2) == 0
                ret(0)
        elif original_dpc and address == BASE+0xed2d0:
            assert reg(0) == 0x40
            ret(dpc_object)
        elif original_dpc and address == STUB+0x40:
            assert [reg(i) for i in range(3)] == [dpc_object, BASE+0xd39380, ROOTCTX]
            dpc_registration.append([reg(1), reg(2)])
            ret(0)
        elif original_dpc and address == STUB+0x44:
            assert [reg(i) for i in range(3)] == [dpc_object, 0, 0]
            inserted = not queue
            if inserted:
                queue.append(dpc_object)
            dpc_events.append(['queue_dpc', inserted])
            ret(int(inserted))
        elif original_dpc and address == BASE+0xd3af88:
            assert reg(0) == PRIMARY+0x5b0 and lock_results
            locked = lock_results.pop(0)
            dpc_events.append(['lock', locked])
            ret(int(locked))
        elif original_dpc and address == BASE+0xd3afc8:
            assert reg(0) == PRIMARY+0x5b0
            dpc_events.append(['unlock'])
            ret(0)
        elif original_dpc and address == BASE+0xd48ce0:
            assert reg(0) == ROOTCTX and reg(1) == PRIMARY
            dpc_events.append(['hal_irq', reg(2)])
        elif address == BASE+0xce4b0:
            ret(0)

    u.hook_add(UC_HOOK_CODE, hook)

    def call(rva, *args):
        for i, value in enumerate(args):
            u.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        u.reg_write(regs.UC_ARM64_REG_SP, STACK+0xf000)
        u.reg_write(regs.UC_ARM64_REG_LR, STOP)
        u.emu_start(BASE+rva, STOP, count=20000)
        assert u.reg_read(regs.UC_ARM64_REG_PC) == STOP
        return reg(0)

    call(0xd4aeb8, CTX, mode)
    if original_primary:
        u.mem_write(PRIMARY+0x496, b'\x01\x00')
        call(0x1951238, PRIMARY)
        assert struct.unpack('<Q', u.mem_read(PRIMARY+0x280, 8))[0] == BASE+0xd48830
        assert struct.unpack('<Q', u.mem_read(PRIMARY+0xe0, 8))[0] == BASE+0xd45670
    offsets = {hex(offset): struct.unpack('<I', u.mem_read(CTX+offset, 4))[0]
               for offset in (0x20, 0x28, 0x30, 0x38, 0x40, 0x44, 0x48, 0x4c)}
    for address, value in ((CTX+8, BASE+0xd3d880), (CTX+0x10, BASE+0xd3d8d0),
                           (CTX+0x18, LINK), (LINK, OWNER), (PARENT+0x60, CTX),
                           (ROOTCTX+0x300, PARENT), (ROOTCTX+0x170, PRIMARY),
                           (PRIMARY+0x280, BASE+0xd48830 if original_primary else STUB+4),
                           (PRIMARY+0xe0, BASE+0xd45670 if original_primary else STUB+8),
                           (PRIMARY+0x68, OUTER+0x1000),
                           (OUTER+0x1000, OUTER+0x2000), (OUTER+0x20b0, STUB+12),
                           (BASE+0x1512148, OWNER if global_present else 0),
                           (ROOTCTX+0x160, 0x12340000)):
        u.mem_write(address, struct.pack('<Q', value))
    u.mem_write(OUTER+0x1054, struct.pack('<I', owned))
    u.mem_write(LINK+8, struct.pack('<I', int(enabled)))
    u.mem_write(CTX+0x40e0, struct.pack('<I', counter))
    u.mem_write(CTX+0x40dc, struct.pack('<I', pending))
    if original_dpc:
        assert original_primary and entry_wrapper and not status
        assert not primary_status & 0x80  # SWGEN1/mailbox paths have separate tests.
        for addr, value in ((PRIMARY, BASE+0x108e9a0),
                            (PRIMARY+8, BASE+0x108e9b8), (PRIMARY+0x68, PRIMARY+8),
                            (ROOTCTX+0x178, PRIMARY+8), (ROOTCTX+0x300, 0),
                            (BASE+0xe3e108, STUB+0x40), (BASE+0xe3e110, STUB+0x44)):
            u.mem_write(addr, struct.pack('<Q', value))
        u.mem_write(PRIMARY+0x5c, struct.pack('<I', owned))
        u.mem_write(PRIMARY+0x5b4, struct.pack('<I', outstanding))
        u.mem_write(PRIMARY+0x524, struct.pack('<I', int(primary_disabled)))
        assert call(0xd3b428, OUTER+0x3000, ROOTCTX, BASE+0xd39380, ROOTCTX+0x160) == 0
        assert dpc_registration == [[BASE+0xd39380, ROOTCTX]]
        if queue_already_pending:
            queue.append(dpc_object)
    result = (call(0xd36ee8, entry_input) if entry_wrapper else
              call(0xd39198, OWNER, ROOTCTX) if parent else call(0xd4b308, CTX))
    pending_before_dpc = struct.unpack('<I', u.mem_read(PRIMARY+0x4a0, 4))[0]
    outstanding_before_dpc = struct.unpack('<I', u.mem_read(PRIMARY+0x5b4, 4))[0]
    dpc_runs = 0
    while queue:
        assert original_dpc and dpc_runs < 2
        queue.pop(0)
        callback, context = dpc_registration[0]
        call(callback-BASE, dpc_object, context, 0, 0)
        dpc_runs += 1
    return {'return': result, 'trace': trace, 'offsets': offsets,
            'counter': struct.unpack('<I', u.mem_read(CTX+0x40e0, 4))[0],
            'pending': struct.unpack('<I', u.mem_read(CTX+0x40dc, 4))[0],
            'primary_calls': primary_calls, 'schedules': len(schedules),
            'schedule_payloads': schedules, 'notifications': notifications,
            'primary_pending': struct.unpack('<I', u.mem_read(PRIMARY+0x4a0, 4))[0],
            'pending_before_dpc': pending_before_dpc, 'dpc_events': dpc_events,
            'outstanding_before_dpc': outstanding_before_dpc,
            'dpc_runs': dpc_runs,
            'outstanding': struct.unpack('<I', u.mem_read(PRIMARY+0x5b4, 4))[0],
            'timestamps': {hex(offset): struct.unpack('<Q', u.mem_read(OWNER+offset, 8))[0]
                           for offset in (0x340, 0x348, 0x3c0, 0x3c8, 0x440, 0x448)}}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
    imports = {e.address-BASE: e.name for dll in pe.DIRECTORY_ENTRY_IMPORT for e in dll.imports}
    assert imports[0xe3e108] == b'KeInitializeDpc'
    assert imports[0xe3e110] == b'KeInsertQueueDpc'
    for table, slot, target in ((0x108e9b8, 0xb0, 0x641b90),
                                (0x108e9b8, 0x218, 0x142470),
                                (0x108e9a0, 0x298, 0xd43fd0)):
        assert struct.unpack('<Q', pe.get_data(table+slot, 8))[0] == BASE+target
    # Static caller edges complement executed wrapper/parent cases; these are
    # not a PDO-to-GSI registration proof.
    for rva, target in ((0xdd21e0, 0xd36ee8), (0xd36fa4, 0xd39198),
                        (0xd36fb4, 0xd3b4a0), (0xd396a8, 0xd3b428)):
        instruction = struct.unpack('<I', pe.get_data(rva, 4))[0]
        assert instruction & 0xfc000000 == 0x94000000
        immediate = instruction & 0x3ffffff
        if immediate & 0x2000000:
            immediate -= 0x4000000
        assert rva+immediate*4 == target
    header = ROOT/'sources/linux-nv-oot-r38/drivers/video/tegra/host/nvdla/port/pm/nvdla_pm_hfrp_reg.h'
    assert hashlib.sha256(header.read_bytes()).hexdigest() == HEADER_HASH
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-') as tmp:
        library, shim = Path(tmp)/'hfrp.so', Path(tmp)/'l4t.c'
        shim.write_text('#include <stdint.h>\n#include "'+str(header)+'"\n'+
                        'void l4t_offsets(uint32_t *v) {\n'+
                        '\n'.join('v[%d]=%s;' % (i, expr) for i, expr in enumerate((
                            'hfrp_buffer_clientoffs_r()', 'hfrp_buffer_cmd_r(0)',
                            'hfrp_buffer_serveroffs_r()', 'hfrp_buffer_resp_r(0)',
                            'hfrp_irq_in_set_r()', 'hfrp_irq_out_set_r()',
                            'hfrp_irq_in_clr_r()', 'hfrp_irq_out_clr_r()')))+'\n}\n')
        subprocess.run(shlex.split(os.environ.get('CC', 'cc'))+[
            '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
            '-I'+str(ROOT/'include'), str(ROOT/'driver/n1x_hfrp.c'), str(shim),
            '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
    take = lib.n1x_hfrp_take_irq
    take.argtypes = [ctypes.POINTER(IO), ctypes.POINTER(Result)]
    offsets = (ctypes.c_uint32*8)()
    lib.l4t_offsets(offsets)
    reference = windows(pe, 0)
    assert list(reference['offsets'].values()) == list(offsets)
    cases = 0
    for status in (0, 1, 2, 3, *(1 << bit for bit in range(26, 32)), 0xfc000003, 0xffffffff):
        for counter in (0, 0xffffffff):
            original = windows(pe, status, counter=counter, pending=0x10)
            assert original['return'] == bool(status)
            assert original['trace'] == [('read', 0x104, status), ('write', 0x10c, status)]
            assert original['counter'] == (counter+bool(status & 2)) & 0xffffffff
            assert original['pending'] == status | 0x10
            for fail_at in (None, 0, 1):
                trace = []

                @READ
                def read(_, offset, out):
                    assert offset == 0x104
                    trace.append(('read', offset, status))
                    out[0] = status
                    return -errno.EIO if fail_at == 0 else 0

                @WRITE
                def write(_, offset, value):
                    assert offset == 0x10c and value == status
                    trace.append(('write', offset, value))
                    return -errno.EIO if fail_at == 1 else 0

                io, result = IO(None, write, read, DELAY()), Result()
                ret = take(ctypes.byref(io), ctypes.byref(result))
                expected = -errno.EIO if fail_at == 0 or (status and fail_at == 1) else (0 if status else -errno.EAGAIN)
                assert ret == expected
                assert result.status_valid == (fail_at != 0)
                assert result.ack_attempted == (fail_at != 0 and bool(status))
                assert result.acknowledged == (fail_at is None and bool(status))
                if result.status_valid:
                    assert result.status == status
                if status and fail_at is None:
                    assert trace == original['trace']
                else:
                    assert trace == original['trace'][:1 if not status or fail_at == 0 else 2]
                cases += 1
    # Windows swallows the lower accessor status; Linux must not do that.
    assert windows(pe, 2, read_error=0x65)['return'] == 1
    assert windows(pe, 2, read_error=0x65, read_output=False)['return'] == 0
    assert windows(pe, 2, write_error=0x65)['return'] == 1
    parent_cases = []
    for enabled in (False, True):
        for status in (0, 2, *(1 << bit for bit in range(26, 32))):
            for primary in (0, 0x40, 0xc0):
                result = windows(pe, status, parent=True, primary_status=primary, enabled=enabled)
                primary_probed = not (enabled and status)
                assert result['primary_calls'] == ([primary] if primary_probed else [])
                assert result['schedules'] == (primary_probed and bool(primary))
                assert result['primary_pending'] == (primary if primary_probed else 0)
                for bit, offset in enumerate((0x340, 0x348, 0x3c0, 0x3c8, 0x440, 0x448), 26):
                    assert result['timestamps'][hex(offset)] == (0x12345678 if enabled and status & (1 << bit) else 0)
                parent_cases.append({'hfrp_enabled': enabled, 'hfrp_status': hex(status),
                                     'primary_status': hex(primary), 'primary_probed': primary_probed})
    accumulated = windows(pe, 1 << 27, pending=1 << 26, parent=True)
    assert accumulated['timestamps']['0x340'] == accumulated['timestamps']['0x348'] == 0x12345678
    result = windows(pe, 0, parent=True, primary_status=0xc0, primary_error=0x65)
    assert result['schedules'] == 0 and result['primary_pending'] == 0
    native_parent_cases = 0
    for enabled in (False, True):
        for status in (0, 2):
            for primary in (0, 0x40, 0x80, 0xc0, 0x100):
                for owned in (0, 0x40, 0xc0):
                    result = windows(pe, status, parent=True, primary_status=primary,
                                     enabled=enabled, original_primary=True, owned=owned)
                    probed = not (enabled and status)
                    assert result['primary_calls'] == ([primary] if probed else [])
                    assert result['primary_pending'] == (primary if probed else 0)
                    assert result['schedule_payloads'] == ([primary & owned] if probed and primary else [])
                    native_parent_cases += 1
    # The entry wrapper ignores its incoming value and uses the shared global
    # owner/parent lookup. This does not identify a physical interrupt number.
    wrapper_cases = 0
    for number in (0, 1, 2, 0x12345678, 0xffffffff):
        for status in (0, 2):
            for primary in (0, 0x40, 0xc0):
                result = windows(pe, status, parent=True, primary_status=primary, original_primary=True,
                                 entry_wrapper=True, entry_input=number, owned=0x40)
                assert result['return'] == 1
                assert result['schedule_payloads'] == ([primary & 0x40] if not status and primary else [])
                assert result['notifications'] == [0x12340000]
                wrapper_cases += 1
    result = windows(pe, 0, parent=True, original_primary=True, entry_wrapper=True,
                     global_present=False)
    assert result['return'] == 0 and not result['primary_calls'] and not result['notifications']
    result = windows(pe, 0, parent=True, primary_status=0xc0, original_primary=True,
                     primary_error=0x65)
    assert result['primary_pending'] == 0 and not result['schedule_payloads']
    dpc_cases = []
    for bits in (0, 0x40, 0x100, 0x140):
        for mask in (0, 0x40):
            for count in (0, 2):
                for already in (False, True):
                    result = windows(pe, 0, original_primary=True, entry_wrapper=True,
                                     original_dpc=True, primary_status=bits, owned=mask,
                                     outstanding=count, queue_already_pending=already)
                    assert result['return'] == 1 and result['dpc_runs'] == 1
                    assert result['pending_before_dpc'] == bits and result['primary_pending'] == 0
                    assert result['outstanding_before_dpc'] == count
                    assert result['outstanding'] == (max(count-1, 0) if bits & 0x40 else count)
                    expected = ([['irq_clear', bits & mask]] if bits else [])
                    expected += [['queue_dpc', not already]]
                    if bits:
                        expected += [['hal_irq', bits], ['lock', True], ['unlock']]
                    assert result['dpc_events'] == expected
                    dpc_cases.append({'bits': bits, 'ownership_mask': mask, 'counter': count,
                                      'already_queued': already, 'events': result['dpc_events']})
    result = windows(pe, 0, original_primary=True, entry_wrapper=True, original_dpc=True,
                     primary_status=0x40, dpc_locks=(False, True))
    assert result['dpc_runs'] == 2 and result['primary_pending'] == 0 and result['outstanding'] == 1
    assert result['dpc_events'] == [
        ['irq_clear', 0x40], ['queue_dpc', True], ['hal_irq', 0x40], ['lock', False],
        ['queue_dpc', True], ['hal_irq', 0x40], ['lock', True], ['unlock']]
    dpc_cases.append({'lock_retry': True, 'events': result['dpc_events']})
    result = windows(pe, 0, original_primary=True, entry_wrapper=True, original_dpc=True,
                     primary_status=0x40, write_error=0x65)
    assert result['outstanding'] == 1 and result['primary_pending'] == 0
    assert result['dpc_events'][0] == ['irq_clear', 0x40]
    dpc_cases.append({'clear_error_ignored': True, 'events': result['dpc_events']})
    result = windows(pe, 0, original_primary=True, entry_wrapper=True, original_dpc=True,
                     primary_status=0x40, primary_disabled=True)
    assert result['outstanding'] == 2 and result['dpc_events'] == [['queue_dpc', True]]
    dpc_cases.append({'primary_disabled': True, 'events': result['dpc_events']})
    for invalid in ('io', 'read', 'write', 'result'):
        io, result = IO(None, WRITE(), READ(), DELAY()), Result()
        ctypes.memset(ctypes.byref(result), 0xa5, ctypes.sizeof(result))
        before = bytes(result)
        if invalid != 'read': io.read32 = READ(lambda _, off, out: 0)
        if invalid != 'write': io.write32 = WRITE(lambda _, off, value: 0)
        assert take(None if invalid == 'io' else ctypes.byref(io),
                    None if invalid == 'result' else ctypes.byref(result)) == -errno.EINVAL
        assert bytes(result) == before
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'windows_initializer': '0xd4aeb8', 'windows_irq_capture': '0xd4b308',
                      'windows_parent_dispatch': '0xd39198', 'l4t_header_sha256': HEADER_HASH,
                      'eight_matching_offsets': dict(zip(reference['offsets'], list(offsets))),
                      'capture_fault_cases': cases, 'parent_dispatch_cases': parent_cases,
                      'original_primary_parent_cases': native_parent_cases,
                      'original_entry_wrapper': '0xd36ee8', 'entry_wrapper_input_cases': wrapper_cases,
                      'primary_callbacks': {'status': '0xd48830', 'clear_dispatch': '0xd45670'},
                      'original_dpc_cases': dpc_cases,
                      'limitations': ['OS/MMIO and owner/parent lookups are synthetic; legacy cases also model the provider callback',
                                      'The additional DPC cases execute original provider/clear/DPC/SWGEN0 code with modeled Ke APIs and locks',
                                      'The DPC cases suppress HFRP and SWGEN1, and do not establish Windows scheduler notification or DMA retirement',
                                      'No ACPI IRQ number or GIC delivery is established',
                                      'HFRP event capture does not implement power, clock or response-ring processing',
                                      'The C helper is not linked to the Linux bootstrap or any live IRQ handler',
                                      'No physical boot, power transitions or inference']}, indent=2))


if __name__ == '__main__':
    main()
