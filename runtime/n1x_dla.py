"""Thin native-runtime binding; explicit prepared-image submission, no retries."""
import ctypes as c
import os
from pathlib import Path


class Range(c.Structure):
    _fields_ = [('offset', c.c_uint64), ('bytes', c.c_uint64)]


class Info(c.Structure):
    _fields_ = [('abi_version', c.c_uint32), ('capabilities', c.c_uint32),
                ('max_image_bytes', c.c_uint32), ('max_addresses', c.c_uint32),
                ('retained_bytes', c.c_uint64), ('retain_limit', c.c_uint64),
                ('retained_jobs', c.c_uint32), ('max_jobs', c.c_uint32),
                ('reserved', c.c_uint64)]


class Result(c.Structure):
    _fields_ = [('image_ptr', c.c_uint64), ('ranges_ptr', c.c_uint64),
                ('writable_ranges_ptr', c.c_uint64),
                ('image_bytes', c.c_uint32), ('num_addresses', c.c_uint32),
                ('num_writable_ranges', c.c_uint32), ('flags', c.c_uint32),
                ('job_id', c.c_uint64), ('status', c.c_int32),
                ('completed', c.c_uint32)]


def checked(code, job_id=0):
    if code < 0:
        error = OSError(-code, os.strerror(-code))
        error.job_id = job_id
        raise error
    return code


class Device:
    def __init__(self, path):
        self.fd = -1
        lib = self.lib = c.CDLL(str(Path(__file__).with_name('libn1x_dla.so')))
        lib.n1x_dla_open.argtypes = [c.c_char_p]
        lib.n1x_dla_info.argtypes = [c.c_int, c.POINTER(Info)]
        lib.n1x_dla_submit.argtypes = [c.c_int, c.c_void_p, c.c_uint,
                                     c.POINTER(Range), c.c_uint,
                                     c.POINTER(Range), c.c_uint, c.POINTER(Result)]
        lib.n1x_dla_read.argtypes = [c.c_int, c.c_ulonglong, c.c_ulonglong,
                                   c.c_void_p, c.c_ulonglong]
        for name in ('open', 'info', 'submit', 'read'):
            getattr(lib, 'n1x_dla_' + name).restype = c.c_int
        self.fd = checked(lib.n1x_dla_open(os.fsencode(path)))

    def info(self):
        result = Info()
        checked(self.lib.n1x_dla_info(self.fd, c.byref(result)))
        return {name: getattr(result, name) for name, _ in Info._fields_}

    def submit(self, image, ranges, writable):
        if (not 0 < len(image) <= 32 << 20 or not 0 < len(ranges) <= 512 or
                not 0 < len(writable) <= 64):
            raise ValueError('image/address count outside ABI limits')
        for offset, size in [*ranges, *writable]:
            if offset < 0 or size <= 0 or offset > len(image) or size > len(image) - offset:
                raise ValueError('address range outside image')
        for offset, size in writable:
            if not any(offset >= base and size <= extent - (offset - base)
                       for base, extent in ranges if offset >= base):
                raise ValueError('writable range is not covered by an address range')
        backing = c.create_string_buffer(bytes(image), len(image))
        addresses = (Range * len(ranges))(*(Range(*item) for item in ranges))
        writable_ranges = (Range * len(writable))(*(Range(*item) for item in writable))
        result = Result()
        checked(self.lib.n1x_dla_submit(self.fd, backing, len(image), addresses,
                                       len(ranges), writable_ranges, len(writable),
                                       c.byref(result)), result.job_id)
        return result.job_id

    def read(self, job_id, offset, size):
        if not 0 < job_id < 1 << 64 or offset < 0 or offset >= 1 << 64 or not 0 < size <= 32 << 20:
            raise ValueError('invalid result range')
        output = c.create_string_buffer(size)
        checked(self.lib.n1x_dla_read(self.fd, job_id, offset, output, size), job_id)
        return output.raw

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
