#!/usr/bin/env python3
"""Verify phase prefixes, evidence gates and no retry after failed writes."""
import errno
from pathlib import Path
import tempfile
from unittest.mock import patch

from qualify_p16_start import PHASES, TRIGGERS, run_phases


def main():
    cases = 0
    with tempfile.TemporaryDirectory(prefix='n1x-sequence-') as tmp:
        dev = Path(tmp)
        for end in range(len(PHASES)):
            for failure in (None, 'write', 'evidence'):
                calls, records = [], []
                (dev/'hfrp_status').write_text('power_accepted=0\n')
                (dev/'status').write_text('attempted=0\n')

                def record(**row):
                    records.append(row)

                def trigger(path, payload):
                    phase = path.name
                    assert records[-1] == {'action': 'trigger', 'phase': phase}
                    assert payload == TRIGGERS[phase]
                    index = len(calls)
                    assert phase == PHASES[index][0]
                    calls.append(phase)
                    if index == end and failure == 'write':
                        raise OSError(errno.EREMOTEIO, 'simulated rejected operation')
                    file = dev/('hfrp_status' if phase == 'prepare_hfrp' else 'status')
                    file.write_text(PHASES[index][1]+'='+('0' if index == end and failure else '1')+'\n')

                caught = None
                with patch('qualify_p16_start.trigger', trigger), patch('qualify_p16_start.os.sync') as sync:
                    try:
                        run_phases(dev, PHASES[end][0] if failure is None else PHASES[-1][0], record)
                    except (OSError, RuntimeError) as exc:
                        caught = exc
                assert calls == [phase for phase, _ in PHASES[:end+1]]
                assert sync.call_count == len(calls)
                if failure == 'write':
                    assert isinstance(caught, OSError) and caught.errno == errno.EREMOTEIO
                    assert records[-1]['errno'] == errno.EREMOTEIO
                elif failure == 'evidence':
                    assert isinstance(caught, RuntimeError)
                else:
                    assert caught is None
                cases += 1
    print(f'{cases} startup prefix/failure cases passed; no root, sysfs or hardware access')


if __name__ == '__main__':
    main()
