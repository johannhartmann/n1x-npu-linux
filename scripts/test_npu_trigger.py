#!/usr/bin/env python3
"""Check unbuffered trigger errors without accessing a device."""
import errno
import os
from unittest.mock import patch

from npu_trigger import trigger


def main():
    cases = 0
    for result in (7, 0, 3, OSError(errno.EREMOTEIO, 'firmware rejected'),
                   OSError(errno.EALREADY, 'already attempted'),
                   InterruptedError(errno.EINTR, 'interrupted')):
        with patch('npu_trigger.os.open', return_value=42) as opened, \
             patch('npu_trigger.os.close') as closed, \
             patch('npu_trigger.os.write') as written:
            if isinstance(result, BaseException):
                written.side_effect = result
            else:
                written.return_value = result
            try:
                trigger('/fake/upload', b'upload\n')
                assert result == 7
            except OSError as error:
                assert error.errno == (result.errno if isinstance(result, OSError) else errno.EIO)
            opened.assert_called_once_with('/fake/upload', os.O_WRONLY | os.O_CLOEXEC)
            written.assert_called_once_with(42, b'upload\n')
            closed.assert_called_once_with(42)
            cases += 1
    with patch('npu_trigger.os.open', side_effect=PermissionError(errno.EACCES, 'denied')), \
         patch('npu_trigger.os.write') as written, patch('npu_trigger.os.close') as closed:
        try:
            trigger('/fake/upload', b'upload\n')
            raise AssertionError('open failure ignored')
        except PermissionError:
            written.assert_not_called()
            closed.assert_not_called()
            cases += 1
    print(f'{cases} unbuffered trigger cases; no device access, buffered flush or explicit write retry')


if __name__ == '__main__':
    main()
