# SPDX-License-Identifier: MIT
"""Image operations around explicit NPU executors; never substitutes CPU inference."""
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps
from superres_image import fixed_tiles

ROOT = Path(__file__).resolve().parents[1]


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
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
    width,height = image.size
    if min(width,height)<=0: raise ValueError('empty image')
    if width<height: size = (256, int(256*height/width))
    else: size = (int(256*width/height),256)
    image = image.resize(size, Image.Resampling.BILINEAR)
    left,top = int(round((image.width-224)/2)),int(round((image.height-224)/2))
    crop = image.crop((left,top,left+224,top+224))
    array = np.asarray(crop, dtype=np.float32).transpose(2,0,1)[None]/255
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
    import onnxruntime as ort
    options = ort.SessionOptions(); options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(ROOT/'sources/vision'/name), sess_options=options,
                                  providers=['CPUExecutionProvider'])
    return session.run(None, {'input':np.ascontiguousarray(data,dtype=np.float32)})[0]


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
