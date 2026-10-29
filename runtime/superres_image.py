"""Image/layout stages shared by the N1X demo and independent reference tests.

The tile executor is explicit: this module never silently substitutes a CPU
reference for DLA inference. Learned execution returns nine luminance planes;
pixel shuffle and chroma resizing are ordinary host-side image operations.
"""
from dataclasses import dataclass
import hashlib
import numpy as np

MODEL_SHA256='85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e'
HALO=5
SCALE=3


@dataclass(frozen=True)
class Tile:
    x:int
    y:int
    width:int
    height:int
    left:int
    top:int
    right:int
    bottom:int

    @property
    def input_shape(self):return self.bottom-self.top,self.right-self.left


def tiles(height,width,core=128):
    if min(height,width,core)<=0:raise ValueError('positive image and tile sizes required')
    for y in range(0,height,core):
        for x in range(0,width,core):
            w,h=min(core,width-x),min(core,height-y)
            yield Tile(x,y,w,h,max(0,x-HALO),max(0,y-HALO),
                       min(width,x+w+HALO),min(height,y+h+HALO))


def fixed_tiles(height,width,size=64,halo=HALO):
    """Cover a full image with one static network shape, preserving real edges.

    Shift the last window to touch the global edge instead of padding its
    source outside the image. Adjacent core boundaries divide any overlap.
    """
    if halo<0 or size<=2*halo or min(height,width)<size:raise ValueError('image smaller than fixed tile or invalid halo')
    def axis(length):
        origins=list(range(0,length-size+1,size-2*halo))
        if origins[-1]!=length-size:origins.append(length-size)
        previous=0
        for i,origin in enumerate(origins):
            end=origins[i+1]+halo if i+1<len(origins) else length
            yield origin,previous,end
            previous=end
    for top,y,end_y in axis(height):
        for left,x,end_x in axis(width):
            yield Tile(x,y,end_x-x,end_y-y,left,top,left+size,top+size)


def tiled_planes(luminance,infer_tile,core=128,plan=None):
    source=np.asarray(luminance,dtype=np.float32)
    if source.ndim!=2 or not np.isfinite(source).all():raise ValueError('finite 2D luminance required')
    height,width=source.shape
    result=np.empty((1,9,height,width),dtype=np.float32)
    for tile in tiles(height,width,core) if plan is None else plan:
        data=np.ascontiguousarray(source[tile.top:tile.bottom,tile.left:tile.right][None,None])
        output=np.asarray(infer_tile(data))
        if output.shape!=(1,9,*tile.input_shape) or not np.isfinite(output).all():
            raise ValueError('invalid tile result')
        y,x=tile.y-tile.top,tile.x-tile.left
        result[:,:,tile.y:tile.y+tile.height,tile.x:tile.x+tile.width]=output[:,:,y:y+tile.height,x:x+tile.width]
    return result


def pixel_shuffle(planes):
    source=np.asarray(planes)
    if source.ndim!=4 or source.shape[:2]!=(1,9):raise ValueError('expected one image and nine planes')
    h,w=source.shape[-2:]
    return source.reshape(1,1,3,3,h,w).transpose(0,1,4,2,5,3).reshape(h*3,w*3)


def open_luminance(path):
    from PIL import Image
    with Image.open(path) as image:
        image.load();rgb=image.convert('RGB')
    y,cb,cr=rgb.convert('YCbCr').split()
    return rgb,np.asarray(y,dtype=np.float32)/255.0,cb,cr


def assemble_rgb(planes,cb,cr):
    from PIL import Image
    y=pixel_shuffle(planes)
    if not np.isfinite(y).all():raise ValueError('nonfinite super-resolution output')
    intensity=Image.fromarray(np.rint(np.clip(y,0,1)*255).astype(np.uint8))
    size=intensity.size
    return Image.merge('YCbCr',(intensity,cb.resize(size,Image.Resampling.BICUBIC),
                               cr.resize(size,Image.Resampling.BICUBIC))).convert('RGB')


class CpuReference:
    """Independent ONNX Runtime CPU executor, exclusively for validation."""
    def __init__(self,path,threads=4):
        import onnx
        import onnxruntime as ort
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=MODEL_SHA256:raise ValueError('source ONNX hash mismatch')
        model=onnx.load_model_from_string(raw)
        nodes=[n for n in model.graph.node if n.op_type in ('Conv','Relu')]
        assert len(nodes)==7 and sum(n.op_type=='Conv' for n in nodes)==4
        del model.graph.node[:];model.graph.node.extend(nodes)
        del model.graph.input[:]
        model.graph.input.extend([onnx.helper.make_tensor_value_info('input',onnx.TensorProto.FLOAT,[1,1,'height','width'])])
        del model.graph.output[:]
        model.graph.output.extend([onnx.helper.make_tensor_value_info(nodes[-1].output[0],onnx.TensorProto.FLOAT,[1,9,'height','width'])])
        del model.graph.value_info[:]
        onnx.checker.check_model(model)
        options=ort.SessionOptions();options.intra_op_num_threads=threads
        self.session=ort.InferenceSession(model.SerializeToString(),sess_options=options,providers=['CPUExecutionProvider'])

    def __call__(self,data):
        return self.session.run(None,{'input':np.ascontiguousarray(data,dtype=np.float32)})[0]
