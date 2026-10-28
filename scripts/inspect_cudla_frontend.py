#!/usr/bin/env python3
"""Check NVIDIA's real Linux cuDLA frontend against a temporary mock backend.

Offline API-boundary research, NOT a functional cuDLA driver. No device access.
Vendor inputs stay under ignored sources/; generated C and libraries are temporary.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile

import pefile

from inspect_cudla_release import BASE, RAM, Machine, ROOT, SHA256

PACKAGE = ROOT / 'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive'
LIB_HASH = '89c7431ded0c95c8cf7e68c878e20396d3d905534a6283a6c48307f09f9798c9'


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=30, **kwargs).stdout


def linux_frontend():
    assert platform.machine() == 'aarch64', 'Native ARM64 execution required'
    library = PACKAGE / 'lib/libcudla.so.1.0.0'
    assert hashlib.sha256(library.read_bytes()).hexdigest() == LIB_HASH
    includes = PACKAGE / 'include'
    declarations = {}
    for name in ('cudla.h', 'cudlaExternalEtbl.hpp'):
        source = re.sub(r'/\*.*?\*/', '', (includes / name).read_text(), flags=re.S)
        for match in re.finditer(r'cudlaStatus\s+(cudla\w+)\s*\(([^;{}]*)\)\s*;', source):
            declarations[match[1]] = match[2]
    assert len(declarations) == 17
    driver_names = set(re.findall(rb'cudlaDrv[A-Z][A-Za-z]+\x00', library.read_bytes()))
    driver_names = {name[:-1].decode() for name in driver_names} - {'cudlaDrvFuncInit'}
    expected = {name.replace('cudla', 'cudlaDrv', 1) for name in declarations}
    assert driver_names == expected | {'cudlaDrvGetExportTable'}
    prefix = '#include <stdint.h>\n#include <stdlib.h>\n#include "cudlaExternalEtbl.hpp"\n'
    definitions, calls = [], []
    for index, (name, arguments) in enumerate(declarations.items()):
        checks, values = [], []
        for position, argument in enumerate(arguments.split(',')):
            argument = argument.strip()
            match = re.fullmatch(r'(.+?)([A-Za-z_]\w*)', argument)
            assert match, argument
            typename, varname = match.groups()
            # Deliberately invalid API inputs test forwarding only. The mock
            # does not dereference them or claim these are valid cuDLA objects.
            value = 0x1000 + index * 0x100 + position * 0x10
            checks.append(f'if ((uintptr_t){varname} != {value}ULL) abort();')
            values.append(f'({typename}){value}ULL')
        driver = name.replace('cudla', 'cudlaDrv', 1)
        definitions.append((driver, f'cudlaStatus {driver}({arguments}) {{ '
                            + ' '.join(checks)
                            + ' return cudlaErrorUnsupportedOperation; }\n'))
        calls.append(f'if ({name}({", ".join(values)}) != cudlaErrorUnsupportedOperation) return {index+1};')
    # This private export lacks a declaration in the public headers. Resolve
    # its symbol, but never call it or claim to have established its signature.
    definitions.append(('cudlaDrvGetExportTable', 'void cudlaDrvGetExportTable(void) { abort(); }\n'))
    with tempfile.TemporaryDirectory(prefix='n1x-cudla-boundary-') as temp:
        directory = Path(temp)
        backend = directory / 'backend.c'
        client = directory / 'client.c'
        client.write_text(prefix + 'int main(void) {\n' + '\n'.join(calls) + '\nreturn 0; }\n')
        def build_backend(omit=None):
            backend.write_text(prefix + ''.join(code for name, code in definitions if name != omit))
            run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror', '-I', str(includes),
                 str(backend), '-o', str(directory / 'libnvcudla.so')])
        build_backend()
        run(['cc', '-Wall', '-Wextra', '-Werror', '-I', str(includes), str(client),
             str(library), '-Wl,-rpath,' + str(library.parent), '-o', str(directory / 'client')])
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('LD_', 'NVTX_'))}
        env['LD_LIBRARY_PATH'] = str(directory)
        run([str(directory / 'client')], env=env)
        # Use a fresh process for each missing symbol: the real frontend caches
        # its loader state. Record the actual failure instead of guessing it.
        client.write_text(prefix + '#include <stdio.h>\nint main(void) { uint64_t v = 0; '
                          'printf("%d\\n", (int)cudlaGetVersion(&v)); return 0; }\n')
        run(['cc', '-I', str(includes), str(client), str(library),
             '-Wl,-rpath,' + str(library.parent), '-o', str(directory / 'client')])
        missing = {}
        for name in sorted(driver_names):
            build_backend(name)
            status = int(run([str(directory / 'client')], env=env).strip())
            assert status == 1, (name, status)
            missing[name] = status
    return {'library_sha256': LIB_HASH, 'forwarded_public_calls': list(declarations),
            'required_driver_exports': sorted(driver_names), 'missing_export_status': missing,
            'limits': 'Mock backend only; no device, NvSci, CUDA or inference implementation'}


def windows_mode_gate():
    path = ROOT / 'sources/nvidia/Display.Driver/nvcudla.dll'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA256
    pe = pefile.PE(str(path))
    exports = {entry.name.decode(): entry.address for entry in pe.DIRECTORY_ENTRY_EXPORT.symbols}
    class ModeMachine(Machine):
        def hook(self, u, pc, size, data):
            if pc == BASE + 0x57640:
                assert self.reg(0) == 8
                self.events.append('allocate_handle')
                self.ret(RAM + 0x1e000)
            elif pc == BASE + 0x57620:
                assert self.reg(0) == RAM + 0x1e000
                self.events.append('free_handle')
                self.ret()
            else:
                super().hook(u, pc, size, data)
    cases = []
    for flag in (1, 3, 4):
        machine = ModeMachine(pe)
        output = RAM + 0x1d000
        machine.put(output, 0xdeadbeef)
        result = machine.call(exports['cudlaDrvCreateDevice'], 0, output, flag)
        assert result == (13 if flag in (1, 3) else 1)
        assert machine.get(output) == 0xdeadbeef
        assert not machine.kernel_envelopes
        cases.append({'flags': flag, 'status': result, 'events': machine.events})
    machine = ModeMachine(pe)
    assert machine.call(exports['cudlaDrvGetNvSciSyncAttributes'], 0, 0) == 13
    return {'driver_sha256': SHA256, 'exports': {k: hex(v) for k, v in exports.items()},
            'create_mode_cases': cases, 'nvscisync_attributes_status': 13,
            'limits': 'Original ARM64 instructions; Windows initialization, locks and allocation modeled; hybrid creation not executed'}


def main():
    linux = linux_frontend()
    windows = windows_mode_gate()
    assert set(linux['required_driver_exports']) == set(windows['exports'])
    print(json.dumps({'linux': linux, 'windows': windows}, indent=2))


if __name__ == '__main__':
    main()
