#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check repository links, source syntax, JSON and published demo provenance.

Standard-library only. Does not import project modules, write bytecode, access
devices, acquire inputs or execute examples. Includes untracked source files so
new contributions can be checked before staging.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'!?\[[^\]]*\]\(([^\s)]+)\)|(?:src|href)="([^"\s]+)"')


def prose(text):
    """Exclude fenced examples from Markdown link/heading interpretation."""
    marker = ''
    result = []
    for line in text.splitlines():
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            run, rest = fence.groups()
            if not marker:
                marker = run
            elif run[0] == marker[0] and len(run) >= len(marker) and not rest.strip():
                marker = ''
            continue
        if not marker:
            result.append(line)
    if marker:
        raise ValueError('unclosed Markdown code fence')
    return '\n'.join(result)


def anchors(text):
    result, seen = set(), set()
    text = prose(text)
    for heading in re.findall(r'^ {0,3}#{1,6}\s+(.+?)\s*#*$', text, re.M):
        heading = re.sub(r'<[^>]*>', '', heading).lower()
        slug = ''.join(c for c in heading if c.isalnum() or c in ' -_').replace(' ', '-')
        candidate, index = slug, 0
        while candidate in seen:
            index += 1
            candidate = f'{slug}-{index}'
        seen.add(candidate)
        result.add(candidate)
    result.update(re.findall(r'(?:id|name)="([^"]+)"', text))
    return result


def link_error(document, target):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    destination = document.parent/unquote(parsed.path) if parsed.path else document
    if not destination.exists():
        return f'missing local link {target}'
    if parsed.fragment and destination.suffix == '.md' and destination.is_file():
        if unquote(parsed.fragment) not in anchors(destination.read_text()):
            return f'missing Markdown section {target}'
    return None


def main():
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT)
    paths = sorted({ROOT/os.fsdecode(name) for name in names.split(b'\0') if name})
    errors = []
    counts = dict(python=0, markdown=0, json=0)
    for path in paths:
        if not path.is_file():  # Deleted/renamed entries not yet staged.
            continue
        relative = path.relative_to(ROOT)
        try:
            if path.suffix == '.py':
                compile(path.read_bytes(), str(relative), 'exec')
                counts['python'] += 1
            elif path.suffix == '.json':
                json.loads(path.read_text())
                counts['json'] += 1
            elif path.suffix == '.md':
                counts['markdown'] += 1
                for match in LINK.finditer(prose(path.read_text())):
                    target = match[1] or match[2]
                    error = link_error(path, target)
                    if error:
                        errors.append(f'{relative}: {error}')
        except (SyntaxError, ValueError, UnicodeError) as exc:
            errors.append(f'{relative}: {exc}')

    receipt = json.loads((ROOT/'docs/evidence/npu-hardware-2026-10-28.json').read_text())
    sample = receipt['full_image_runs'][0]
    for name, expected in (('astronaut-input.png', sample['input_sha256']),
                           ('astronaut-dla-3x.png', sample['output_sha256'])):
        path = ROOT/'demo/assets'/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f'{path.relative_to(ROOT)}: differs from the physical qualification receipt')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Repository checks passed: {counts["python"]} Python files, '
          f'{counts["markdown"]} Markdown files, {counts["json"]} JSON records; '
          'published sample/result hashes match physical evidence; no hardware access')


if __name__ == '__main__':
    main()
