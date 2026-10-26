#!/usr/bin/env python3
"""Check append-only test entry generation preserves all original menu bytes."""
from install_sroot_test import render_entry, EFI_NAME


def main():
    before = 'timeout: 5\nquiet: no\ndefault_entry: Omarchy/linux-p16-asus-23\n/+Omarchy\n  //linux-p16-asus-23\n'
    pin = 'a'*128
    for text in (before, before.rstrip('\n')):
        after = render_entry(text, pin, 'root=/dev/mapper/root acpi=nospcr')
        assert after.startswith(text)
        assert after.count('default_entry:') == after.count('timeout:') == 1
        assert '/N1X NPU test\n' in after
        assert EFI_NAME+'#'+pin in after
    for text, digest, cmd in ((before, 'bad', 'ok'), (before, pin, 'bad\ninject'),
                              (before.replace('Omarchy/linux-p16-asus-23', 'other'), pin, 'ok'),
                              (before+'/N1X NPU test\n', pin, 'ok'),
                              (before+EFI_NAME, pin, 'ok')):
        try:
            render_entry(text, digest, cmd)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid entry input accepted')
    npu1 = render_entry(before, pin, 'root=/dev/mapper/root')
    npu2 = render_entry(npu1, pin, 'root=/dev/mapper/root keep_bootcon n1x_bootlog=1',
                        '7.2.8-23-omarchy-p16-asus-npu2')
    assert npu2.startswith(npu1)
    assert 'omarchy_linux-p16-asus-23-npu2.efi#' + pin in npu2
    assert npu2.count('default_entry:') == npu2.count('timeout:') == 1
    try:
        render_entry(npu2, pin, 'keep_bootcon n1x_bootlog=1', '7.2.8-23-omarchy-p16-asus-npu2')
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate npu2 entry accepted')
    print('npu1/npu2 append-only menu fixtures and rejection cases passed; no boot writes')


if __name__ == '__main__':
    main()
