#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile vision partitions through the checked native/Windows bridge; no device access."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from vision_graph import MODELS

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--example', choices=('denoise','classify','both'), default='both')
    args = parser.parse_args()
    models = [m for m in MODELS if args.example=='both' or m.startswith('denoise-')==(args.example=='denoise')]
    expected = json.loads((ROOT/'config/vision-models.json').read_text())
    for name,sha in expected['prepared'].items():
        if hashlib.sha256((ROOT/'sources/vision'/name).read_bytes()).hexdigest()!=sha:
            parser.error('prepared input differs from pinned manifest: '+name)
    paths = [ROOT/'results'/('native-compile-t254-'+m) for m in models]
    paths += [ROOT/'results'/('windows-'+m+suffix) for m in models for suffix in ('-pretranslation','-backend')]
    paths += [ROOT/'results'/('build-'+m+'.log') for m in models]
    out = ROOT/'results'/('vision-build-'+args.example+'.json')
    paths.append(out)
    if any(p.exists() for p in paths):
        parser.error('existing compilation results preserved; use a fresh workspace')
    sys.path.insert(0, str(ROOT/'runtime'))
    from loadable import Model
    receipt = {}
    for model in models:
        logpath = ROOT/'results'/('build-'+model+'.log')
        with logpath.open('x') as log:
            def run(script, *args):
                print(model+': '+script+' '+' '.join(args), flush=True)
                subprocess.run([sys.executable, str(ROOT/'scripts'/script), *args], cwd=ROOT,
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            graph = ('--model', model, '--target', 't254')
            bridge = ('--model', model)
            run('probe_native_graph.py', *graph, '--capture-mtl')
            run('compile_superres_backend.py', *bridge)
            run('probe_native_graph.py', *graph, '--capture-translation-input')
            run('compile_superres_backend.py', *bridge, '--translate')
            run('probe_native_graph.py', *graph, '--package-windows-output')
            run('compile_superres_backend.py', *bridge, '--translate')
            run('probe_native_graph.py', *graph, '--package-windows-output')
        path = ROOT/'results'/('native-compile-t254-'+model)/(model+'.nvda')
        compiled = Model.from_file(path)
        receipt[model] = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             image_bytes=len(compiled.image), retained_bytes=(len(compiled.image)+8192+4095)&~4095,
                             hardware_access=False)
        if any(receipt[model][key]!=expected['models'][model][key]
               for key in ('sha256','image_bytes','retained_bytes')):
            raise ValueError(model+': build differs from the pinned artifact; inspect '+str(logpath))
    out.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__=='__main__': main()
