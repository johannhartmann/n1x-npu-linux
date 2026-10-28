#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build checked fixture loadables in a fresh workspace, without device access.

Runs native graph construction in the existing device-free bubblewrap sandbox
and Windows backend translation in Unicorn. Refuses existing output directories;
never overwrites a previously qualified model. See docs/setup.md for inputs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'relu': '561fe58bafa90f845580492044d7ca9dae0c250878a38751ef34feb8f857a942',
    'conv1x1': '717de7a60381d210d0fbf5df72b66cbc8650dca536b801ef9719de8ed3c4487b',
    'conv1x1_relu': 'fe57704f93edbafe9e374e14675cfc34cc5a44858497c6126dec56a60a837536',
    'superres': 'c1ffde5d1eb9c7486a08ecc1470a1b633a11c7a71411f8fb36064d16dbcfc1c0',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--superres', action='store_true', help='also build the experimental super-resolution graph')
    args = parser.parse_args()
    if platform.machine() != 'aarch64':
        parser.error('native graph construction requires an ARM64 Linux host')
    if not all(shutil.which(name) for name in ('bwrap', 'gdb')):
        parser.error('install bubblewrap and gdb first')
    models = ['relu', 'conv1x1', 'conv1x1_relu']+(['superres'] if args.superres else [])
    outputs = ['native-compile-t254'+('' if m == 'relu' else '-'+m) for m in models]
    outputs += ['windows-pretranslation-output', 'windows-final-backend-output',
                'relu-probe', 'p16-relu-template.bin', 'model-build.log', 'model-build.jsonl']
    existing = [p for p in outputs if (ROOT/'results'/p).exists()]
    if existing:
        parser.error('use a fresh workspace; existing outputs were not changed: '+', '.join(existing))
    subprocess.run([sys.executable, str(ROOT/'scripts/prepare_inputs.py')], check=True)
    (ROOT/'results').mkdir(exist_ok=True)
    with (ROOT/'results/model-build.log').open('xb') as log, \
         (ROOT/'results/model-build.jsonl').open('x', buffering=1) as receipt:
        def run(script, *arguments):
            command = [sys.executable, 'scripts/'+script, *arguments]
            event = json.dumps(dict(command=command, hardware_access=False))
            receipt.write(event+'\n');receipt.flush();os.fsync(receipt.fileno())
            print(' '.join(command), flush=True)
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)

        for model in models:
            graph = ['--model', model, '--target', 't254']
            run('probe_native_graph.py', *graph, '--capture-mtl')
            bridge = ('inspect_windows_mtl_collector.py' if model == 'relu' else
                      'compile_superres_backend.py' if model == 'superres' else 'inspect_conv_pretranslation.py')
            bridge_args = [] if model in ('relu', 'superres') else ['--model', model]
            run(bridge, *bridge_args)
            run('probe_native_graph.py', *graph, '--capture-translation-input')
            run(bridge, *bridge_args, '--translation-input' if model == 'relu' else '--translate')
            run('probe_native_graph.py', *graph, '--package-windows-output')
            if model == 'superres':
                # Bootstrap serialization supplies exact packed-constant extents.
                # Replay original Windows packed constants before final packaging.
                run(bridge, '--translate')
                run('probe_native_graph.py', *graph, '--package-windows-output')
            elif model == 'relu':
                run('verify_relu_loadable.py')
                run('create_relu_probe.py')
                run('prepare_p16_relu.py')
            else:
                run('verify_conv_loadable.py', *bridge_args)
            directory = 'native-compile-t254'+('' if model == 'relu' else '-'+model)
            path = ROOT/'results'/directory/(model+'.nvda')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != EXPECTED[model]:
                raise ValueError(f'{model}: unexpected artifact hash; inspect results/model-build.log')
            receipt.write(json.dumps(dict(model=model, sha256=digest, artifact=str(path.relative_to(ROOT)),
                                          hardware_access=False))+'\n')
        receipt.write(json.dumps(dict(complete=True, models=models, hardware_access=False))+'\n')
        receipt.flush();os.fsync(receipt.fileno())


if __name__ == '__main__':
    main()
