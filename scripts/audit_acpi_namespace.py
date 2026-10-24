#!/usr/bin/env python3
"""Index decompiled ASL namespace declarations for manual power-path review.

This is an aid for reviewing iasl output, not an AML interpreter or a safety
certificate. In particular it does not evaluate conditions, aliases or methods.
"""
import argparse
import json
import re
from pathlib import Path

MASK = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', re.S)
BLOCK = re.compile(r'\b(Scope|Device|PowerResource|Method)\s*\(\s*([\\^A-Za-z0-9_.]+)[^)]*\)\s*\{')
NAME = re.compile(r'\b(Name|Alias|External)\s*\(\s*([\\^A-Za-z0-9_.]+)')


def path_for(current, name):
    parts = [] if name.startswith('\\') else list(current)
    name = name.lstrip('\\')
    while name.startswith('^'):
        if not parts:
            raise ValueError('namespace parent traversal above root')
        parts.pop()
        name = name[1:]
    return tuple(parts+[segment.ljust(4, '_') for segment in name.split('.') if segment])


def declarations(path):
    text = path.read_text()
    masked = MASK.sub(lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), text)
    blocks = {m.end()-1: m for m in BLOCK.finditer(masked)}
    names = {m.start(): m for m in NAME.finditer(masked)}
    events = sorted([(m.start(), m[0]) for m in re.finditer('[{}]', masked)]+
                    [(position, 'name') for position in names])
    stack = [()]
    rows = []
    for position, event in events:
        if event == '}':
            if len(stack) == 1:
                raise ValueError(f'unbalanced braces in {path}')
            stack.pop()
        elif event == '{':
            declaration = blocks.get(position)
            current = stack[-1]
            if declaration:
                current = path_for(current, declaration[2])
                rows.append((current, declaration[1], text.count('\n', 0, declaration.start())+1))
            stack.append(current)
        else:
            declaration = names[position]
            rows.append((path_for(stack[-1], declaration[2]), declaration[1],
                         text.count('\n', 0, position)+1))
    if len(stack) != 1:
        raise ValueError(f'unbalanced braces in {path}')
    return text, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    target = ('_SB_', 'DLA0')
    powers = {'_PS0', '_PS3', '_PR0', '_PR3', '_DEP', '_PSC', '_PRW'}
    paths = list(sorted(args.directory.glob('*.dsl')))
    if not paths:
        parser.error('no decompiled tables')
    report = []
    for path in paths:
        text, rows = declarations(path)
        relevant = []
        for namespace, kind, line in rows:
            if namespace[:len(target)] == target or (
                    namespace and namespace[-1] in powers and
                    namespace[:-1] in ((), target[:1], target)):
                relevant.append({'path': '\\'+'.'.join(namespace), 'kind': kind, 'line': line})
        report.append({'file': str(path), 'indexed_declarations': len(rows),
                       'relevant_declarations': relevant,
                       'literal_dla0_lines': [i for i, line in enumerate(text.splitlines(), 1) if 'DLA0' in line]})
    print(json.dumps({'scope': 'Static decompiled ASL index for human review; no AML evaluation',
                      'target': '\\_SB_.DLA0', 'tables': report,
                      'limitations': ['Aliases, conditions and method side effects require manual review',
                                      'Archived candidates do not prove the live firmware contents']}, indent=2))


if __name__ == '__main__':
    main()
