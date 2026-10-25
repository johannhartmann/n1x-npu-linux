#!/usr/bin/env python3
"""Replay the Windows state writer and its actual start dispatch in RAM only."""
import hashlib
import json

import pefile
from extract_firmware import KMD_SHA256
from test_cold_start_dispatch import ROOT, run


def expected_events(old, target, mask, runtime, notify):
    # The original comparison of the old state is signed, not an enum check.
    signed_old = old if old < 0x80000000 else old - 0x100000000
    if old == target or (signed_old > 0 and target != 0):
        return None
    events = []
    if target == 0:
        if mask & 32:
            if old == 3:
                events += ['hfrp_startup']
                if runtime:
                    events += ['resume_setup']
            if runtime:
                events += ['power_on', 'loader']
        elif mask & 16:
            events += ['power_on']
    else:
        if old == 0:
            if mask & 32:
                events += ['stop']
            elif mask & 16:
                events += ['power_on']
        if notify:
            events += ['notify']
    return events


def main():
    path = ROOT/'sources/nvidia/Display.Driver/nvlddmkm.sys'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == KMD_SHA256
    pe = pefile.PE(str(path), fast_load=True)
    cases = failures = 0
    examples = []
    for old in (0, 1, 2, 3, 0xffffffff):
        for target in (0, 1, 2, 3, 0xffffffff):
            for mask in (0, 16, 32, 48):
                for runtime in (False, True):
                    for notify in (False, True):
                        events = expected_events(old, target, mask, runtime, notify)
                        result = run(pe, 0xd40d50, phase=old, target=target,
                                     policy_mask=mask, runtime=runtime, notify=notify)
                        if events is None:
                            assert result['status'] == 0x3f and not result['events'], result
                            assert result['state'] == [old, 0x12345678, 0xabcdef01], result
                        else:
                            assert result['status'] == 0 and result['events'] == events, result
                            assert result['state'] == [target, old, target], result
                            for event in result['observations']:
                                expected = [target, old, target] if event['event'] == 'notify' else [old, 0x12345678, target]
                                assert event['state'] == expected, result
                                if event['event'] == 'power_on':
                                    assert event['power_mask'] == (7 if mask & 32 else 1 if target == 0 else 16), result
                        cases += 1
    # Original updates software state even when start/stop fails. Verify that
    # the callback still sees the OLD state, explaining the 3 -> 0 restart.
    for old, target, mask, events in (
        (3, 0, 32, ['hfrp_startup', 'resume_setup', 'power_on', 'loader']),
        (1, 0, 32, ['power_on', 'loader']),
        (0, 3, 32, ['stop', 'notify']),
        (3, 0, 16, ['power_on']),
        (0, 3, 16, ['power_on', 'notify']),
    ):
        for fail in events:
            result = run(pe, 0xd40d50, phase=old, target=target, policy_mask=mask,
                         notify=True, fail=fail)
            expected = events[:events.index(fail)+1]
            if target == 0 and mask & 32 and fail in ('power_on', 'loader'):
                expected += ['cleanup']
            if target and fail != 'notify':
                expected += ['notify']
            assert result['events'] == expected, result
            assert result['status'] == (0 if fail == 'notify' else 0x65), result
            assert result['state'] == [target, old, target], result
            failures += 1
            examples.append(result)
    print(json.dumps({'passed': True, 'hardware_access': False,
                      'kmd_sha256': KMD_SHA256, 'transition_cases': cases,
                      'failure_cases': failures, 'failure_examples': examples,
                      'limitations': [
                          'Start dispatch executes unchanged; HFRP, power, loader and cleanup bodies are stubs',
                          'Stop and notification are explicit boundaries; no physical suspend or resume',
                          'Numeric state 3 is not independently identified as a Windows or ACPI enum',
                          'This proves a state producer, not a full cold-boot baseline or a Linux recovery procedure']}, indent=2))


if __name__ == '__main__':
    main()
