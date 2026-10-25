#!/usr/bin/env python3
"""Trace original boot transport, including modeled IRP completion/error handling."""
import hashlib
import json
import struct
import pefile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as regs
from arm64_stack_cookie import install as install_stack_cookie
from extract_firmware import KMD_SHA256
from emulate_firmware_commands import ROOT

BASE = 0x140000000
CTX, HANDLE, DATA, STACK, STUB = 0x21000000, 0x21100000, 0x21200000, 0x21300000, 0x21400000


def run(pe, subtype=0, available=True, length=16, completion=None,
        pending=False, wait_status=0, retries=1, build_fail=False):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for page, size in ((0xd49000, 0x2000), (0xd3b000, 0x1000), (0xd3c000, 0x1000),
                       (0xd57000, 0x1000), (0xd39000, 0x1000),
                       (0x1512000, 0x1000), (0xe3e000, 0x1000),
                       (0xd38000, 0x1000), (0x13ac000, 0x1000)):
        u.mem_map(BASE+page, size)
        if page in (0xd49000, 0xd3b000, 0xd3c000):
            u.mem_write(BASE+page, pe.get_data(page, size))
    for address, size in ((CTX, 0x1000), (HANDLE, 0x1000), (DATA, 0x1000), (STACK, 0x10000), (STUB, 0x1000)):
        u.mem_map(address, size)
    ret = bytes.fromhex('c0035fd6')
    for rva in (0xd39160, 0xd38758, 0xd38a38):
        u.mem_write(BASE+rva, ret)
    for address in (STUB, STUB+0x10, STUB+0x20, STUB+0x30, STUB+0x40, STUB+0x50):
        u.mem_write(address, ret)
    u.mem_write(BASE+0xe3e180, struct.pack('<Q', STUB))
    u.mem_write(BASE+0xe3e450, struct.pack('<Q', STUB+0x10))
    for slot, target in ((0xe3e008, 0x20), (0xe3e458, 0x30),
                         (0xe3e9e8, 0x40), (0xe3eac0, 0x50)):
        u.mem_write(BASE+slot, struct.pack('<Q', STUB+target))
    u.mem_write(BASE+0x13ac13c, struct.pack('<I', 5))  # Disable diagnostic logging.
    u.mem_write(CTX+0x308, struct.pack('<Q', HANDLE if available else 0))
    u.mem_write(CTX+0x50, struct.pack('<II', 12345, retries))
    u.mem_write(HANDLE+0x18, struct.pack('<Q', 0x21500000))
    u.mem_write(DATA, bytes(range(16)))
    calls, events = [], []
    io_status = None
    canceled = False

    def reg(index):
        return u.reg_read(getattr(regs, f'UC_ARM64_REG_X{index}'))

    def hook(_, address, size, user):
        nonlocal io_status, canceled
        if address == BASE+0xd39160:
            u.reg_write(regs.UC_ARM64_REG_X0, CTX)
        elif address in (BASE+0xd38758, BASE+0xd38a38):
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB:
            assert reg(0) == HANDLE+0x20 and reg(1) == reg(2) == 0
            events.append('initialize_event')
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x10:
            calls.append({'ioctl': hex(reg(0)), 'device_object': hex(reg(1)),
                          'input': bytes(u.mem_read(reg(2), reg(3))).hex(), 'input_bytes': reg(3),
                          'output_pointer': reg(4), 'output_bytes': reg(5), 'internal': reg(6)})
            if completion is None:
                u.emu_stop()
            else:
                assert reg(7) == HANDLE+0x20
                io_status = struct.unpack('<Q', u.mem_read(u.reg_read(regs.UC_ARM64_REG_SP), 8))[0]
                assert bytes(u.mem_read(io_status, 16)) == bytes(16)
                canceled = False
                events.append('build_irp')
                u.reg_write(regs.UC_ARM64_REG_X0, 0 if build_fail else DATA+0x800)
        elif address == STUB+0x20:
            u.mem_write(reg(0), struct.pack('<Q', 10000000))
            u.reg_write(regs.UC_ARM64_REG_X0, 0)
        elif address == STUB+0x30:
            assert io_status and reg(0) == 0x21500000 and reg(1) == DATA+0x800
            events.append('call_driver')
            u.mem_write(io_status, struct.pack('<I', completion))
            u.reg_write(regs.UC_ARM64_REG_X0, 0x103 if pending else completion)
        elif address == STUB+0x40:
            assert reg(0) == HANDLE+0x20 and reg(1) == reg(2) == reg(3) == 0
            if canceled:
                assert reg(4) == 0
                events.append('wait_after_cancel_without_timeout')
                result = 0
            else:
                assert struct.unpack('<q', u.mem_read(reg(4), 8))[0] == -123450
                events.append('wait_for_completion')
                result = wait_status
            u.reg_write(regs.UC_ARM64_REG_X0, result)
        elif address == STUB+0x50:
            assert reg(0) == DATA+0x800
            events.append('cancel_irp')
            canceled = True
            u.reg_write(regs.UC_ARM64_REG_X0, 1)

    install_stack_cookie(u, pe)
    u.hook_add(UC_HOOK_CODE, hook)
    for index, value in enumerate((CTX, CTX, DATA, length, subtype)):
        u.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
    u.reg_write(regs.UC_ARM64_REG_SP, STACK+0x8000)
    u.reg_write(regs.UC_ARM64_REG_LR, STUB+0xff0)
    u.emu_start(BASE+0xd49ef0, STUB+0xff0, count=10000)
    returned = u.reg_read(regs.UC_ARM64_REG_PC) == STUB+0xff0
    return {'returned': returned, 'status': reg(0) if returned else None,
            'os_boundary': calls, 'events': events}


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path))
    imports = {item.address: item.name for entry in pe.DIRECTORY_ENTRY_IMPORT for item in entry.imports}
    assert imports[BASE+0xe3e180] == b'KeInitializeEvent'
    assert imports[BASE+0xe3e450] == b'IoBuildDeviceIoControlRequest'
    assert imports[BASE+0xe3e458] == b'IofCallDriver'
    assert imports[BASE+0xe3e008] == b'KeQueryPerformanceCounter'
    assert imports[BASE+0xe3e9e8] == b'KeWaitForSingleObject'
    assert imports[BASE+0xe3eac0] == b'IoCancelIrp'
    rows = []
    for subtype in (0, 1):
        result = run(pe, subtype)
        expected = {'ioctl': '0x150214' if subtype == 0 else '0x150218',
                    'device_object': '0x21500000',
                    'input': bytes(range(16)).hex() if subtype == 0 else '02030c0002000000',
                    'input_bytes': 16 if subtype == 0 else 8,
                    'output_pointer': 0, 'output_bytes': 0, 'internal': 0}
        assert not result['returned'] and result['os_boundary'] == [expected]
        rows.append({'subtype': subtype, **result})
    result = run(pe, available=False)
    assert result['returned'] and result['status'] == 0xffff and not result['os_boundary']
    rows.append({'case': 'missing_auxiliary_handle', **result})
    result = run(pe, length=0)
    assert result['returned'] and result['status'] == 0x1f and not result['os_boundary']
    rows.append({'case': 'empty_image_rejected', **result})
    completions = []
    for subtype in (0, 1):
        for completion in (0, 0xc0000229):
            for pending in (False, True):
                result = run(pe, subtype, completion=completion, pending=pending)
                assert result['returned'] and result['status'] == completion
                assert result['events'] == ['initialize_event', 'build_irp', 'call_driver']+(
                    ['wait_for_completion'] if pending else [])
                assert len(result['os_boundary']) == 1
                completions.append({'subtype': subtype, 'completion': hex(completion),
                                    'pending': pending, **result})
    result = run(pe, completion=0xc0000229, pending=True, wait_status=0x102, retries=3)
    assert result['returned'] and result['status'] == 0xc0000229
    assert len(result['os_boundary']) == 3
    assert result['events'] == ['initialize_event', 'build_irp', 'call_driver',
        'wait_for_completion', 'cancel_irp', 'wait_after_cancel_without_timeout']*3
    completions.append({'case': 'timeout_cancel_wait_and_retry', **result})
    result = run(pe, completion=0xc0000229, pending=True, wait_status=0xc0000001)
    assert result['returned'] and result['status'] == 0x65
    assert result['events'] == ['initialize_event', 'build_irp', 'call_driver', 'wait_for_completion']
    completions.append({'case': 'unexpected_wait_result', **result})
    result = run(pe, completion=0, build_fail=True)
    assert result['returned'] and result['status'] == 0xffff
    assert result['events'] == ['initialize_event', 'build_irp']
    completions.append({'case': 'irp_allocation_failure', **result})
    # Compose original-code results across the modeled OS boundary: recorded
    # SROOT reply -> provider NTSTATUS -> KMD transport -> variant selector.
    from test_sroot_full_upload import windows as upload, Bus, LIVE_REPLY
    from test_sroot_send import HASH as SROOT_HASH
    from emulate_boot_variants import run as select_variant
    provider = ROOT/'sources/platform-drivers/Windows/System32/DriverStore/FileRepository/nvsrootmbx.inf_arm64_6ee85afcd10dd594/nvsrootmbx.sys'
    assert hashlib.sha256(provider.read_bytes()).hexdigest() == SROOT_HASH
    provider_pe = pefile.PE(str(provider), fast_load=True)
    image = pe.get_data(0x1405800, 67508)
    provider_result = upload(provider_pe, image, Bus(16, LIVE_REPLY, []), 0)
    assert provider_result['return'] == 0xc0000229
    status_chain = []
    for pending in (False, True):
        transport = run(pe, completion=provider_result['return'], pending=pending)
        assert transport['status'] == provider_result['return']
        for previous_success in (False, True):
            selection = select_variant(pe, 1, fail_requests=1,
                previous_success=previous_success, request_error=transport['status'])
            assert selection['status'] == (0xc0000229 if previous_success else 0)
            assert [entry['rva'] for entry in selection['requests']] == (
                ['0x1405800'] if previous_success else ['0x1405800', '0x13f5040'])
            assert selection['saved_variant'] == (0 if previous_success else 1)
            status_chain.append({'pending': pending, 'previous_success': previous_success,
                'provider_status': hex(provider_result['return']),
                'transport_status': hex(transport['status']), 'selector': selection})
    print(json.dumps({'passed': True, 'hardware_access': False, 'kmd_sha256': KMD_SHA256,
                      'wrapper': '0xd49ef0', 'transport': '0xd3bbc8', 'cases': rows,
                      'modeled_completion_cases': completions,
                      'recorded_reply_status_chain': status_chain,
                      'limitations': ['OS IRP/event/cancel/time and logging calls are synthetic',
                                      'Synchronous and pending completion values are injected',
                                      'Status chain composes separate emulators; successful second upload is a stub',
                                      'Cancellation completion is modeled, not proof of physical DMA quiescence',
                                      'No additional firmware status/detail channel is passed through these IOCTLs',
                                      'Hardware trust, image acceptance and Linux recovery remain unverified']}, indent=2))


if __name__ == '__main__':
    main()
