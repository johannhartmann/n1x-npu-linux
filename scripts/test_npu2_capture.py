#!/usr/bin/env python3
"""Check capture options and append-only boot entry without touching /boot."""
from install_npu2_capture import append_entry, TITLE, EFI
from prepare_npu2_capture import capture_cmdline, PARAMETER


def main():
    original = 'root=/dev/mapper/root acpi_osi="!Windows 2022" earlycon=efifb keep_bootcon n1x_bootlog=1'
    options = capture_cmdline(original)
    assert options == original + ' log_buf_len=4M ' + PARAMETER
    for invalid in (original + ' ' + PARAMETER, original + ' log_buf_len=1M',
                    original.replace('keep_bootcon', ''), original.replace('n1x_bootlog=1', '')):
        try:
            capture_cmdline(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid source command line accepted')
    before = 'timeout: 5\ndefault_entry: Omarchy/linux-p16-asus-23\n/Existing diagnostic\nprotocol: efi\n'
    after = append_entry(before, 'a' * 128, options)
    assert after.startswith(before) and after.count('timeout:') == 1
    assert '/' + TITLE + '\n' in after and EFI + '#' + 'a' * 128 in after
    for menu, checksum, cmd in ((after, 'a' * 128, options), (before, 'bad', options),
                                (before, 'a' * 128, options + '\ninjected')):
        try:
            append_entry(menu, checksum, cmd)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid menu replacement accepted')
    print('npu2 capture options and append-only menu checks passed')


if __name__ == '__main__':
    main()
