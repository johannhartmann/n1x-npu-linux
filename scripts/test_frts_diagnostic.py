#!/usr/bin/env python3
"""Boot-option and menu-replacement tests; no boot partition writes."""
import unittest
from prepare_frts_diagnostic import diagnostic_cmdline
from install_frts_diagnostic import replace_entry, TITLE


class DiagnosticTests(unittest.TestCase):
    def test_preserves_root_and_quoted_acpi(self):
        original = ('root=/dev/mapper/root acpi_osi="!Windows 2022" quiet splash '
                    'loglevel=0 systemd.show_status=false rd.udev.log_level=0 '
                    'n1x_frts_snapshot=p16-bios300')
        result = diagnostic_cmdline(original)
        self.assertIn('root=/dev/mapper/root acpi_osi="!Windows 2022"', result)
        self.assertNotIn('n1x_frts_snapshot', result)
        self.assertNotIn('quiet', result.split())
        self.assertNotIn('splash', result.split())
        self.assertIn('earlycon=efifb', result.split())
        self.assertEqual([s for s in result.split() if s.startswith('loglevel=')], ['loglevel=7'])

    def test_replaces_only_capture_entry(self):
        prefix = 'timeout: 5\ndefault_entry: Omarchy/linux-p16-asus-23\n/Working\nprotocol: efi\n'
        old = '/N1X FRTS capture\npath: boot():/EFI/Linux/omarchy_n1x-frts-capture.efi#' + 'a' * 128 + '\n'
        suffix = '/Other\nprotocol: efi\n'
        after = replace_entry(prefix + old + suffix, 'a' * 128, 'b' * 128, 'earlycon=efifb')
        self.assertTrue(after.startswith(prefix))
        self.assertTrue(after.endswith(suffix))
        self.assertIn('/' + TITLE + '\n', after)
        self.assertNotIn('/N1X FRTS capture\n', after)

    def test_rejects_bad_entries_and_options(self):
        before = '/N1X FRTS capture\npath: omarchy_n1x-frts-capture.efi#' + 'a' * 128 + '\n'
        for menu, checksum, options in (
                (before, 'a' * 127, 'earlycon=efifb'),
                (before, 'b' * 128, 'earlycon=efifb'),
                (before * 2, 'a' * 128, 'earlycon=efifb'),
                (before, 'a' * 128, 'earlycon=efifb n1x_frts_snapshot=p16-bios300'),
                (before, 'a' * 128, 'quiet'),
                (before, 'a' * 128, 'earlycon=efifb\n')):
            with self.subTest(options=options), self.assertRaises(ValueError):
                replace_entry(menu, checksum, 'b' * 128, options)


if __name__ == '__main__':
    unittest.main()
