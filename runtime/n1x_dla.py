"""Thin native-runtime binding; explicit prepared-image submission, no retries."""
import ctypes as c
import os
from pathlib import Path
import numpy as np


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


class Stats(c.Structure):
    _fields_ = [('job_id', c.c_uint64), ('offset', c.c_uint64), ('bytes', c.c_uint64),
                ('count', c.c_uint32), ('metadata_offset', c.c_uint32),
                ('metadata_stride', c.c_uint32), ('data_offset', c.c_uint32),
                ('data_stride', c.c_uint32), ('reserved', c.c_uint32)]


class Control(c.Structure):
    _fields_ = [('job_id', c.c_uint64), ('action', c.c_uint32), ('reserved', c.c_uint32),
                ('status', c.c_int32), ('outcome', c.c_uint32),
                ('accepted', c.c_uint32), ('suspended', c.c_uint32),
                ('completed', c.c_uint32), ('preempt_completed', c.c_uint32)]


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
        lib.n1x_dla_submit_flags.argtypes = [c.c_int, c.c_void_p, c.c_uint,
                                           c.POINTER(Range), c.c_uint,
                                           c.POINTER(Range), c.c_uint,
                                           c.c_uint, c.POINTER(Result)]
        lib.n1x_dla_read.argtypes = [c.c_int, c.c_ulonglong, c.c_ulonglong,
                                   c.c_void_p, c.c_ulonglong]
        lib.n1x_dla_stats.argtypes = [c.c_int, c.c_ulonglong, c.c_ulonglong,
                                    c.c_ulonglong, c.POINTER(Stats)]
        lib.n1x_dla_control.argtypes = [c.c_int, c.c_ulonglong, c.c_uint,
                                      c.POINTER(Control)]
        for name in ('open', 'info', 'submit', 'submit_flags', 'read', 'stats', 'control'):
            getattr(lib, 'n1x_dla_' + name).restype = c.c_int
        self.fd = checked(lib.n1x_dla_open(os.fsencode(path)))

    def info(self):
        result = Info()
        checked(self.lib.n1x_dla_info(self.fd, c.byref(result)))
        return {name: getattr(result, name) for name, _ in Info._fields_}

    def submit(self, image, ranges, writable, flags=0):
        if (not 0 < len(image) <= 32 << 20 or not 0 < len(ranges) <= 512 or
                not 0 < len(writable) <= 64 or flags & ~1):
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
        if flags:
            checked(self.lib.n1x_dla_submit_flags(self.fd, backing, len(image), addresses,
                                                  len(ranges), writable_ranges, len(writable),
                                                  flags, c.byref(result)), result.job_id)
        else:
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

    def stats(self, job_id, offset, size):
        if not 0 < job_id < 1 << 64 or offset < 0 or offset >= 1 << 64 or not 0 < size <= 32 << 20:
            raise ValueError('invalid stats range')
        out = Stats()
        checked(self.lib.n1x_dla_stats(self.fd, job_id, offset, size, c.byref(out)), job_id)
        return {name: getattr(out, name) for name, _ in Stats._fields_}

    def control(self, job_id, action):
        if not 0 < job_id < 1 << 64 or action not in (1, 2, 3, 4):
            raise ValueError('invalid control request')
        out = Control()
        rc = self.lib.n1x_dla_control(self.fd, job_id, action, c.byref(out))
        if not (action == 2 and rc == -115):
            checked(rc, job_id)
        return {name: getattr(out, name) for name, _ in Control._fields_}

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _broadcast_scale(scale, shape):
    arr = np.asarray(scale, dtype=np.float32)
    if arr.ndim == 1 and len(shape) == 4 and arr.shape[0] == shape[1]:
        return arr.reshape(1, shape[1], 1, 1)
    return arr


def quantize_int8(fp_array, scale=1.0, zero_point=0):
    source = np.asarray(fp_array, dtype=np.float32)
    if not np.isfinite(source).all():
        raise ValueError('invalid non-finite input for INT8 quantization')
    s = _broadcast_scale(scale, source.shape)
    zp = _broadcast_scale(zero_point, source.shape)
    return np.clip(np.rint(source / s + zp), -128, 127).astype(np.int8)


def dequantize_int8(int8_array, scale=1.0, zero_point=0):
    source = np.asarray(int8_array, dtype=np.float32)
    s = _broadcast_scale(scale, source.shape)
    zp = _broadcast_scale(zero_point, source.shape)
    return ((source - zp) * s).astype(np.float32)


def _pack_grouped(nchw, group_channels, dtype, stride_w=None, stride_h=None, total_bytes=None):
    source = np.asarray(nchw, dtype=dtype)
    if source.ndim != 4 or source.shape[0] != 1 or min(source.shape) <= 0:
        raise ValueError('expected (1, C, H, W) tensor')
    if np.issubdtype(source.dtype, np.floating) and not np.isfinite(source).all():
        raise ValueError('invalid non-finite input tensor')
    _, channels, h, w = source.shape
    elem_bytes = np.dtype(dtype).itemsize
    pixel_group_bytes = group_channels * elem_bytes
    groups = (channels + group_channels - 1) // group_channels
    line = int(stride_w) if stride_w is not None else w * pixel_group_bytes
    surface = int(stride_h) if stride_h is not None else h * line
    required = (groups - 1) * surface + (h - 1) * line + (w - 1) * pixel_group_bytes + pixel_group_bytes
    size = int(total_bytes) if total_bytes is not None else max(required, groups * surface)
    if line < w * pixel_group_bytes or surface < h * line or required > size:
        raise ValueError('tensor strides outside allocation')
    result = bytearray(size)
    view = np.ndarray(
        (groups, h, w, group_channels),
        dtype=dtype,
        buffer=result,
        strides=(surface, line, pixel_group_bytes, elem_bytes),
    )
    for ch in range(channels):
        view[ch // group_channels, :, :, ch % group_channels] = source[0, ch]
    return bytes(result)


def _unpack_grouped(packed, shape, group_channels, dtype, stride_w=None, stride_h=None):
    n, channels, h, w = (int(x) for x in shape)
    if n != 1 or min(channels, h, w) <= 0:
        raise ValueError('expected (1, C, H, W) tensor shape')
    elem_bytes = np.dtype(dtype).itemsize
    pixel_group_bytes = group_channels * elem_bytes
    groups = (channels + group_channels - 1) // group_channels
    line = int(stride_w) if stride_w is not None else w * pixel_group_bytes
    surface = int(stride_h) if stride_h is not None else h * line
    required = (groups - 1) * surface + (h - 1) * line + (w - 1) * pixel_group_bytes + pixel_group_bytes
    backing = bytearray(packed)
    if line < w * pixel_group_bytes or surface < h * line or required > len(backing):
        raise ValueError('tensor strides outside allocation')
    view = np.ndarray(
        (groups, h, w, group_channels),
        dtype=dtype,
        buffer=backing,
        strides=(surface, line, pixel_group_bytes, elem_bytes),
    )
    result = np.empty((1, channels, h, w), dtype=dtype)
    for ch in range(channels):
        result[0, ch] = view[ch // group_channels, :, :, ch % group_channels]
    return result


def pack_chw32_int8(nchw, stride_w=None, stride_h=None, total_bytes=None):
    return _pack_grouped(nchw, 32, '<i1', stride_w=stride_w, stride_h=stride_h, total_bytes=total_bytes)


def unpack_chw32_int8(packed, shape, stride_w=None, stride_h=None):
    return _unpack_grouped(packed, shape, 32, '<i1', stride_w=stride_w, stride_h=stride_h)


def pack_chw16_int8(nchw, stride_w=None, stride_h=None, total_bytes=None):
    return _pack_grouped(nchw, 16, '<i1', stride_w=stride_w, stride_h=stride_h, total_bytes=total_bytes)


def unpack_chw16_int8(packed, shape, stride_w=None, stride_h=None):
    return _unpack_grouped(packed, shape, 16, '<i1', stride_w=stride_w, stride_h=stride_h)


def pack_chw16_fp16(nchw, stride_w=None, stride_h=None, total_bytes=None):
    return _pack_grouped(nchw, 16, '<f2', stride_w=stride_w, stride_h=stride_h, total_bytes=total_bytes)


def unpack_chw16_fp16(packed, shape, stride_w=None, stride_h=None):
    out = _unpack_grouped(packed, shape, 16, '<f2', stride_w=stride_w, stride_h=stride_h)
    if not np.isfinite(out).all():
        raise ValueError('nonfinite DLA result')
    return out.astype(np.float32)


def _desc_metadata(desc):
    shape = tuple(int(x) for x in desc.shape)
    size = int(getattr(desc, 'size', None) or getattr(desc, 'bytes'))
    codes = tuple(int(x) for x in (getattr(desc, 'codes', None) or getattr(desc, 'format_codes')))
    strides = tuple(int(x) for x in desc.strides)
    return shape, size, codes, strides


def pack_tensor_for_desc(array, desc, scale=None, zero_point=0):
    shape, size, codes, strides = _desc_metadata(desc)
    data_format, data_type = codes[0], codes[1]
    pixel_format = codes[3] if len(codes) > 3 else 0
    element, line, surface = strides[0], strides[1], strides[2]
    if data_type == 2 and data_format == 3 and element == 2:
        return pack_chw16_fp16(array, stride_w=line, stride_h=surface, total_bytes=size)
    if data_type == 4 and data_format in (3, 5) and element == 1:
        arr = np.asarray(array)
        if np.issubdtype(arr.dtype, np.floating):
            arr = quantize_int8(arr, scale=1.0 if scale is None else scale, zero_point=zero_point)
        else:
            arr = arr.astype(np.int8, copy=False)
        if data_format == 5 or pixel_format == 36:
            return pack_chw32_int8(arr, stride_w=line, stride_h=surface, total_bytes=size)
        return pack_chw16_int8(arr, stride_w=line, stride_h=surface, total_bytes=size)
    raise ValueError(f'unsupported tensor descriptor layout codes={codes} strides={strides}')


def unpack_tensor_from_desc(raw_bytes, desc, scale=None, zero_point=0):
    shape, size, codes, strides = _desc_metadata(desc)
    if len(raw_bytes) != size:
        raise ValueError('packed tensor size mismatch')
    data_format, data_type = codes[0], codes[1]
    pixel_format = codes[3] if len(codes) > 3 else 0
    element, line, surface = strides[0], strides[1], strides[2]
    if data_type == 2 and data_format == 3 and element == 2:
        return unpack_chw16_fp16(raw_bytes, shape, stride_w=line, stride_h=surface)
    if data_type == 4 and data_format in (3, 5) and element == 1:
        if data_format == 5 or pixel_format == 36:
            out = unpack_chw32_int8(raw_bytes, shape, stride_w=line, stride_h=surface)
        else:
            out = unpack_chw16_int8(raw_bytes, shape, stride_w=line, stride_h=surface)
        if scale is not None:
            return dequantize_int8(out, scale=scale, zero_point=zero_point)
        return out
    raise ValueError(f'unsupported tensor descriptor layout codes={codes} strides={strides}')
