#!/usr/bin/env python3
"""Check early-CPIO truncation/name/duplicate rejection without boot activity."""
from verify_sroot_uki import early_files


def entry(name, payload=b''):
    fields = [0, 0o100644, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name)+1, 0]
    data = b'070701'+''.join(f'{v:08x}' for v in fields).encode()+name.encode()+b'\0'
    data += bytes((-len(data))%4)
    data += payload
    return data+bytes((-len(data))%4)


def main():
    archive = entry('kernel/firmware/acpi/test.aml', b'table')+entry('TRAILER!!!')
    assert early_files(archive)['kernel/firmware/acpi/test.aml'] == b'table'
    bad_name = bytearray(archive)
    bad_name[94:102] = b'00000000'
    bad_hex = bytearray(archive)
    bad_hex[6:14] = b'not-hex!'
    cases = [archive[:50], archive[:140], bytes(bad_name), bytes(bad_hex),
             entry('duplicate')+entry('duplicate')+entry('TRAILER!!!'),
             entry('no-trailer'), b'070799'+bytes(110)]
    for index, data in enumerate(cases):
        try:
            early_files(data)
        except ValueError:
            continue
        raise AssertionError(f'malformed CPIO fixture {index} accepted')
    print('Early CPIO valid fixture and seven rejection cases passed; no boot changes')


if __name__ == '__main__':
    main()
