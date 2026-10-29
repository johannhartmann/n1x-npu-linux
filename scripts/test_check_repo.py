#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression checks for local documentation links and section anchors."""
from pathlib import Path
import tempfile

from check_repo import anchors, link_error, prose


def main():
    text = '''# Intro
## Native `cuDLA`: state?
## Wiederholung
## Wiederholung
## Wiederholung-1
## Größe und Fuß
```sh
# This is a shell comment
[example](not-a-document.md)
```
~~~text
## This is an example
~~~
<a id="explicit-target"></a>
'''
    assert anchors(text) == {'intro', 'native-cudla-state', 'wiederholung',
                             'wiederholung-1', 'wiederholung-1-1', 'größe-und-fuß',
                             'explicit-target'}
    assert 'not-a-document' not in prose(text)
    for broken in ('```sh\necho example\n', '~~~\nexample\n'):
        try:
            prose(broken)
        except ValueError:
            pass
        else:
            raise AssertionError('unclosed code fence accepted')
    with tempfile.TemporaryDirectory(prefix='n1x-doc-links-') as tmp:
        directory = Path(tmp)
        document = directory/'guide.md'
        document.write_text(text)
        (directory/'space name.md').write_text('# Example\n')
        (directory/'image.png').write_bytes(b'example')
        for target in ('#intro', '#gr%C3%B6%C3%9Fe-und-fu%C3%9F', '#explicit-target',
                       'space%20name.md#example', 'image.png', 'https://example.org/#remote'):
            assert link_error(document, target) is None, target
        for target in ('missing.md', '#not-a-heading', 'space%20name.md#missing'):
            assert link_error(document, target), target
    print('Documentation links, Unicode/duplicate anchors and fenced-example checks passed; no network or hardware')


if __name__ == '__main__':
    main()
