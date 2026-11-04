#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check repository Markdown links, Python syntax, JSON files, and demo asset hashes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'!?\[[^\]]*\]\(([^\s)]+)\)|(?:src|href)="([^"\s]+)"')
DEMO_ASSETS = {
    'astronaut-input.png': '88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5',
    'astronaut-dla-3x.png': '7613dd25e1f01e1d495f256f32a48537465471c3a7a3fe5d38bb69843ab44ab5',
    'astronaut-noisy-gray.png': '7c5e85d4a542c74d6227a15972e58796e53b6f476c11e69bd46544588e93d4f3',
    'astronaut-denoised-dla.png': '0806ffc3bd55925c0456b22e5884c8626eb2a2c2ec19b9813d444b57d1e7b280',
    'emotion-ferplus-gallery-dla.png': '933733179625438bcec69679960cbf8309ae223007540af39d84c19831740dc0',
}


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
    if parsed.scheme == 'file':
        return f'non-portable file:// link {target}'
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

    for name, expected in DEMO_ASSETS.items():
        path = ROOT/'demo/assets'/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f'{path.relative_to(ROOT)}: SHA-256 mismatch (expected {expected})')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Repository checks passed: {counts["python"]} Python files, '
          f'{counts["markdown"]} Markdown files, {counts["json"]} JSON files; '
          'demo asset hashes verified; no hardware access')


if __name__ == '__main__':
    main()
