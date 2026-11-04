# SPDX-License-Identifier: MIT
"""Image tiling, im2col/pooling, and quality metrics for DnCNN and ResNet-18."""
from pathlib import Path
import struct
import zlib
import numpy as np
from superres_image import fixed_tiles

ROOT = Path(__file__).resolve().parents[1]


def _decode_png_rgb(path):
    raw = Path(path).read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('unsupported image format without Pillow')
    pos = 8
    w = h = color_type = 0
    idat = bytearray()
    while pos < len(raw):
        length, tag = struct.unpack('>I4s', raw[pos:pos+8])
        chunk = raw[pos+8:pos+8+length]
        pos += 12 + length
        if tag == b'IHDR':
            w, h, bit_depth, color_type, comp, filt, interlace = struct.unpack('>IIBBBBB', chunk)
            if bit_depth != 8 or color_type not in (2, 6) or comp != 0 or filt != 0 or interlace != 0:
                raise ValueError('unsupported PNG format without Pillow')
        elif tag == b'IDAT':
            idat.extend(chunk)
        elif tag == b'IEND':
            break
    bpp = 3 if color_type == 2 else 4
    stride = w * bpp
    decomp = zlib.decompress(bytes(idat))
    out = np.empty((h, stride), dtype=np.uint8)
    for y in range(h):
        f = decomp[y * (stride + 1)]
        row = np.frombuffer(decomp, dtype=np.uint8, count=stride, offset=y * (stride + 1) + 1).copy()
        prev = out[y - 1] if y > 0 else np.zeros(stride, dtype=np.uint8)
        if f == 1:
            for i in range(bpp, stride): row[i] = (int(row[i]) + int(row[i - bpp])) & 0xFF
        elif f == 2:
            row = ((row.astype(np.uint16) + prev.astype(np.uint16)) & 0xFF).astype(np.uint8)
        elif f == 3:
            for i in range(stride):
                a = int(row[i - bpp]) if i >= bpp else 0
                row[i] = (int(row[i]) + ((a + int(prev[i])) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = int(row[i - bpp]) if i >= bpp else 0
                b = int(prev[i])
                c = int(prev[i - bpp]) if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (int(row[i]) + pr) & 0xFF
        out[y] = row
    return out.reshape(h, w, bpp)[:, :, :3]


def _resize_bilinear_rgb(rgb, new_w, new_h):
    h, w, c = rgb.shape
    ys = np.clip((np.arange(new_h, dtype=np.float32) + 0.5) * (h / new_h) - 0.5, 0, h - 1)
    xs = np.clip((np.arange(new_w, dtype=np.float32) + 0.5) * (w / new_w) - 0.5, 0, w - 1)
    y0 = np.floor(ys).astype(np.int32); y1 = np.minimum(y0 + 1, h - 1); wy = (ys - y0)[:, None, None]
    x0 = np.floor(xs).astype(np.int32); x1 = np.minimum(x0 + 1, w - 1); wx = (xs - x0)[None, :, None]
    top = rgb[y0][:, x0] * (1.0 - wx) + rgb[y0][:, x1] * wx
    bot = rgb[y1][:, x0] * (1.0 - wx) + rgb[y1][:, x1] * wx
    return np.rint(top * (1.0 - wy) + bot * wy).astype(np.uint8)


def im2col(data, kernel, padding, stride):
    data = np.asarray(data, dtype=np.float32)
    if data.ndim!=4 or data.shape[0]!=1 or not np.isfinite(data).all():
        raise ValueError('expected finite NCHW batch one')
    padded = np.pad(data, ((0,0),(0,0),(padding,padding),(padding,padding)))
    patches = np.lib.stride_tricks.sliding_window_view(padded, (kernel,kernel), axis=(-2,-1))
    patches = patches[:,:,::stride,::stride]
    n,c,h,w,kh,kw = patches.shape
    return np.ascontiguousarray(patches.transpose(0,1,4,5,2,3).reshape(n,c*kh*kw,h,w))


def maxpool(data):
    source = np.pad(data, ((0,0),(0,0),(1,1),(1,1)), constant_values=-np.inf)
    windows = np.lib.stride_tricks.sliding_window_view(source, (3,3), axis=(-2,-1))
    return np.ascontiguousarray(windows[:,:,::2,::2].max(axis=(-1,-2)))


def classify_input(path):
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
        width,height = image.size
        if min(width,height)<=0: raise ValueError('empty image')
        if width<height: size = (256, int(256*height/width))
        else: size = (int(256*width/height),256)
        image = image.resize(size, Image.Resampling.BILINEAR)
        left,top = int(round((image.width-224)/2)),int(round((image.height-224)/2))
        crop = image.crop((left,top,left+224,top+224))
        raw_crop = np.asarray(crop, dtype=np.float32)
    except ImportError:
        rgb = _decode_png_rgb(path)
        height, width = rgb.shape[:2]
        if min(width,height)<=0: raise ValueError('empty image')
        if width<height: new_w, new_h = 256, int(256*height/width)
        else: new_w, new_h = int(256*width/height), 256
        resized = _resize_bilinear_rgb(rgb, new_w, new_h)
        left,top = int(round((new_w-224)/2)),int(round((new_h-224)/2))
        crop = resized[top:top+224, left:left+224]
        raw_crop = crop.astype(np.float32)
    array = raw_crop.transpose(2,0,1)[None]/255
    mean = np.array([.485,.456,.406], dtype=np.float32)[None,:,None,None]
    std = np.array([.229,.224,.225], dtype=np.float32)[None,:,None,None]
    return crop, np.ascontiguousarray((array-mean)/std)


def resnet18(data, infer, weights):
    """NPU convolutions; host im2col, max/average pool, residuals and FC."""
    patches = im2col(data, 7, 3, 2)
    current = np.empty((1,64,112,112),dtype=np.float32)
    for y in range(0,112,28):
        for x in range(0,112,28):
            current[:,:,y:y+28,x:x+28] = infer('resnet18-stem',np.ascontiguousarray(patches[:,:,y:y+28,x:x+28]))
    current = maxpool(current)
    for i in range(8):
        residual = current
        source = im2col(current,3,1,2) if i in (2,4,6) else current
        branch = infer(f'resnet18-b{i}', source)
        if i in (2,4,6): residual = infer(f'resnet18-down{i}', np.ascontiguousarray(current[:,:,::2,::2]))
        if branch.shape!=residual.shape: raise ValueError('residual branch shape mismatch')
        current = np.maximum(branch+residual, np.float32(0))
    pooled = current.mean(axis=(-2,-1))
    return pooled@weights['fc.weight'].T+weights['fc.bias']


def denoise(data, infer, progress=None):
    source = np.asarray(data, dtype=np.float32)
    if source.ndim!=2 or not np.isfinite(source).all(): raise ValueError('finite grayscale input required')
    plan = list(fixed_tiles(*source.shape,96,halo=17))
    noise = np.empty(source.shape, dtype=np.float32)
    for index,tile in enumerate(plan,1):
        current = np.ascontiguousarray(source[None,None,tile.top:tile.bottom,tile.left:tile.right])
        for part in range(3): current = infer(f'denoise-{part}',current)
        if current.shape!=(1,1,96,96) or not np.isfinite(current).all(): raise ValueError('invalid predicted noise')
        y,x = tile.y-tile.top,tile.x-tile.left
        noise[tile.y:tile.y+tile.height,tile.x:tile.x+tile.width] = current[0,0,y:y+tile.height,x:x+tile.width]
        if progress: progress(index,len(plan))
    return source-noise,noise,len(plan)


def reference(name, data):
    path = ROOT/'sources/vision'/name
    inp = np.ascontiguousarray(data, dtype=np.float32)
    try:
        import onnxruntime as ort
        options = ort.SessionOptions(); options.intra_op_num_threads = 4
        session = ort.InferenceSession(str(path), sess_options=options,
                                       providers=['CPUExecutionProvider'])
        return session.run(None, {'input': inp})[0]
    except ImportError:
        import onnx
        from onnx.reference import ReferenceEvaluator
        model = onnx.load(str(path))
        if model.opset_import:
            model.opset_import[0].version = max(model.opset_import[0].version, 14)
        return np.asarray(ReferenceEvaluator(model).run(None, {'input': inp})[0], dtype=np.float32)


def psnr(actual, expected):
    mse = float(np.mean((np.asarray(actual,dtype=np.float64)-expected)**2))
    return 100.0 if mse==0 else float(-10*np.log10(mse))


def ssim(actual, expected):
    """Standard 11x11 Gaussian (sigma 1.5), valid interior, range 1, population variance."""
    x = np.arange(11,dtype=np.float64)-5
    g = np.exp(-(x*x)/(2*1.5**2)); g /= g.sum()
    def filt(a):
        rows = np.lib.stride_tricks.sliding_window_view(a,11,axis=0)
        rows = np.tensordot(rows,g,axes=(-1,0))
        return np.tensordot(np.lib.stride_tricks.sliding_window_view(rows,11,axis=1),g,axes=(-1,0))
    a,b = np.asarray(actual,dtype=np.float64),np.asarray(expected,dtype=np.float64)
    ma,mb = filt(a),filt(b)
    va,vb,cov = filt(a*a)-ma*ma,filt(b*b)-mb*mb,filt(a*b)-ma*mb
    return float(np.mean(((2*ma*mb+.01**2)*(2*cov+.03**2))/
                        ((ma*ma+mb*mb+.01**2)*(va+vb+.03**2))))
