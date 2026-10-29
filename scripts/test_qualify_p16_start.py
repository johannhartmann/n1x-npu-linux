#!/usr/bin/env python3
"""Verify phase prefixes, evidence gates and no retry after failed writes."""
import errno
from pathlib import Path
import tempfile
from unittest.mock import patch

from qualify_p16_start import PHASES, TRIGGERS, ROOT, run_phases, phase_status_file, preflight_accel
import sys
sys.path.insert(0, str(ROOT/'runtime'))
import n1x_dla


def main():
    cases = 0
    with tempfile.TemporaryDirectory(prefix='n1x-sequence-') as tmp:
        dev = Path(tmp)
        for after in (False, True):
            for end in range(int(after), len(PHASES)):
                for failure in (None, 'write', 'evidence'):
                    calls, records = [], []
                    (dev/'hfrp_status').write_text('power_accepted=1 configured=1 phase=5 error=0\n' if after else 'power_accepted=0\n')
                    (dev/'status').write_text('attempted=0\n')
                    (dev/'relu_status').write_text('numeric_verified=0\n')

                    def record(**row):
                        records.append(row)

                    def trigger(path, payload):
                        phase = path.name
                        assert records[-1] == {'action': 'trigger', 'phase': phase}
                        assert payload == TRIGGERS[phase]
                        index = len(calls)+int(after)
                        assert phase == PHASES[index][0]
                        calls.append(phase)
                        if index == end and failure == 'write':
                            raise OSError(errno.EREMOTEIO, 'simulated rejected operation')
                        file = dev/phase_status_file(phase)
                        file.write_text(PHASES[index][1]+'='+('0' if index == end and failure else '1')+'\n')

                    caught = None
                    with patch('qualify_p16_start.trigger', trigger), patch('qualify_p16_start.os.sync') as sync:
                        try:
                            run_phases(dev, PHASES[end][0] if failure is None else PHASES[-1][0], record, after_hfrp=after)
                        except (OSError, RuntimeError) as exc:
                            caught = exc
                    assert calls == [phase for phase, _ in PHASES[int(after):end+1]]
                    assert sync.call_count == len(calls)
                    if failure == 'write':
                        assert isinstance(caught, OSError) and caught.errno == errno.EREMOTEIO
                        assert records[-1]['errno'] == errno.EREMOTEIO
                    elif failure == 'evidence':
                        assert isinstance(caught, RuntimeError)
                    else:
                        assert caught is None
                    cases += 1
        valid = 'power_accepted=1 configured=1 phase=5 error=0'
        for hfrp, status, end in (
                ('', 'attempted=0', 'upload'),
                (valid.replace('power_accepted=1', 'power_accepted=0'), 'attempted=0', 'upload'),
                (valid.replace('configured=1', 'configured=0'), 'attempted=0', 'upload'),
                (valid.replace('phase=5', 'phase=4'), 'attempted=0', 'upload'),
                (valid.replace('error=0', 'error=-5'), 'attempted=0', 'upload'),
                (valid, 'attempted=1', 'upload'), (valid, '', 'upload'),
                (valid, 'attempted=0', 'prepare_hfrp')):
            (dev/'hfrp_status').write_text(hfrp)
            (dev/'status').write_text(status)
            with patch('qualify_p16_start.trigger') as send, patch('qualify_p16_start.os.sync') as sync:
                try:
                    run_phases(dev, end, lambda **row: None, after_hfrp=True)
                except ValueError:
                    pass
                else:
                    raise AssertionError('invalid continuation accepted')
                send.assert_not_called()
                sync.assert_not_called()
            cases += 1
        accel_class=dev/'class'; accel_class.mkdir()
        node=accel_class/'accel9';node.mkdir();(node/'device').symlink_to(dev)
        for fail in (False,True):
            records=[]
            class Device:
                def __init__(self,path):
                    assert path=='/dev/accel/accel9'
                    if fail:raise OSError(errno.EINVAL,'modeled DRM open failure')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def info(self):return {'abi_version':2}
            with patch.object(n1x_dla,'Device',Device):
                try:preflight_accel(dev,lambda **row:records.append(row),accel_class)
                except OSError:assert fail and not records
                else:assert not fail and records==[{'action':'accel ABI preflight','info':{'abi_version':2}}]
            cases+=1
        (node/'device').unlink()
        with patch.object(n1x_dla,'Device') as opened:
            try:preflight_accel(dev,lambda **_:None,accel_class)
            except RuntimeError:pass
            else:raise AssertionError('accepted missing endpoint')
            opened.assert_not_called()
        cases+=1
    print(f'{cases} startup prefix/failure cases passed; no root, sysfs or hardware access')


if __name__ == '__main__':
    main()
