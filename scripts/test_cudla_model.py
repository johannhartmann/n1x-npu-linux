#!/usr/bin/env python3
"""Differentially exercise the actual C++ cuDLA loader against vendor fixtures.

Reuses existing malformed-model suites; no CUDA, device or hardware access.
"""
import contextlib
import ctypes as c
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
import loadable
import test_native_loadable
import verify_conv_loadable


class Desc(c.Structure):
    _fields_ = [('name', c.c_char*81), ('size', c.c_uint64),
                ('n', c.c_uint64), ('channels', c.c_uint64), ('h', c.c_uint64), ('w', c.c_uint64),
                ('codes', c.c_uint8*5), ('strides', c.c_uint32*8)]


class Tensor(c.Structure):
    _fields_ = [('desc', Desc), ('offset', c.c_uint64), ('bind', c.c_uint16)]


class Range(c.Structure):
    _fields_ = [('offset', c.c_uint64), ('bytes', c.c_uint64)]


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-cudla-model-') as tmp:
        directory = Path(tmp)
        source = directory/'bridge.cpp'
        source.write_text(r'''
#include "cudla_model.hpp"
#include <stdexcept>
extern "C" {
void *parse(const uint8_t *data,size_t bytes) {
    try { return new n1x::Model(data,bytes); }
    catch(const std::invalid_argument&) { return nullptr; }
}
void destroy(n1x::Model *m) { delete m; }
const uint8_t *image(n1x::Model *m) { return m->image.data(); }
size_t image_size(n1x::Model *m) { return m->image.size(); }
size_t range_count(n1x::Model *m) { return m->ranges.size(); }
const n1x_dla_accel_range *ranges(n1x::Model *m) { return m->ranges.data(); }
size_t tensor_count(n1x::Model *m,int output) { return (output?m->outputs:m->inputs).size(); }
const n1x::Tensor *tensors(n1x::Model *m,int output) { return (output?m->outputs:m->inputs).data(); }
size_t tensor_size() { return sizeof(n1x::Tensor); }
}
''')
        library = directory/'model.so'
        subprocess.run(['c++', '-std=c++17', '-shared', '-fPIC', '-O1', '-g',
                        '-fsanitize=undefined', '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(ROOT/'include'), '-I'+str(ROOT/'runtime'),
                        '-I'+str(ROOT/'sources/cudla-13.4.92/libcudla-linux-sbsa-13.4.92-archive/include'),
                        str(source), str(ROOT/'runtime/cudla_model.cpp'), '-o', str(library)], check=True)
        lib = c.CDLL(str(library))
        lib.parse.argtypes = [c.c_void_p,c.c_size_t]; lib.parse.restype=c.c_void_p
        for name, restype in [('destroy',None), ('image',c.c_void_p), ('image_size',c.c_size_t),
                              ('range_count',c.c_size_t), ('ranges',c.POINTER(Range))]:
            fn=getattr(lib,name); fn.argtypes=[c.c_void_p]; fn.restype=restype
        lib.tensor_count.argtypes=[c.c_void_p,c.c_int]; lib.tensor_count.restype=c.c_size_t
        lib.tensors.argtypes=[c.c_void_p,c.c_int]; lib.tensors.restype=c.POINTER(Tensor)
        lib.tensor_size.restype=c.c_size_t
        assert lib.tensor_size()==c.sizeof(Tensor)
        passed=rejected=0
        class Checked(loadable.Model):
            def __init__(self,data):
                nonlocal passed,rejected
                raw=c.create_string_buffer(bytes(data))
                model=lib.parse(raw,len(data))
                try:
                    try:
                        super().__init__(data)
                    except loadable.LoadableError:
                        assert not model, 'C++ accepted Python-rejected input'
                        rejected+=1
                        raise
                    assert model, 'C++ rejected supported fixture'
                    assert lib.image_size(model)==len(self.image)
                    assert c.string_at(lib.image(model),len(self.image))==self.image
                    assert lib.range_count(model)==len(self.ranges)
                    assert tuple((lib.ranges(model)[i].offset,lib.ranges(model)[i].bytes)
                                 for i in range(len(self.ranges)))==self.ranges
                    for output,items in enumerate((self.inputs,self.outputs)):
                        assert lib.tensor_count(model,output)==len(items)
                        for i,item in enumerate(items):
                            t=lib.tensors(model,output)[i]; d=t.desc
                            assert (d.name.decode(),d.size,t.offset)==(item.name,item.bytes,item.offset)
                            assert (d.n,d.channels,d.h,d.w)==item.shape
                            assert tuple(d.codes)==item.format_codes and tuple(d.strides)==item.strides
                    passed+=1
                finally:
                    if model:lib.destroy(model)
        with patch.object(loadable,'Model',Checked), patch.object(test_native_loadable,'Model',Checked):
            with contextlib.redirect_stdout(io.StringIO()):
                test_native_loadable.main()
                for variant in ('conv1x1','conv1x1_relu'):
                    with patch.object(sys,'argv',['verify_conv_loadable','--model',variant]):
                        verify_conv_loadable.main()
        print(f'C++ cuDLA loader: {passed} valid and {rejected} malformed cases; exact images/ranges/tensors, UBSan; no hardware')


if __name__=='__main__':
    main()
