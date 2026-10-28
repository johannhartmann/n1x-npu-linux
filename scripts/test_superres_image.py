#!/usr/bin/env python3
"""Check the actual image stitching against independent whole-image ONNX.

CPU reference only: does not claim NPU execution or install/load a driver.
"""
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from superres_image import CpuReference,pixel_shuffle,tiled_planes,tiles,fixed_tiles


def main():
    reference=CpuReference(ROOT/'sources/demo-research/super-resolution-10.onnx')
    rng=np.random.default_rng(373)
    results=[]
    for height,width,core in ((28,31,16),(64,64,32),(219,327,128),(512,512,128),(1024,1024,128)):
        source=rng.random((height,width),dtype=np.float32)
        # Hard transitions expose seam and image-edge handling errors.
        source[height//3:height//2,width//4:width//2]=1
        source[0,:]=0;source[-1,:]=1;source[:,0]=1;source[:,-1]=0
        started=time.monotonic()
        whole=reference(source[None,None])
        tiled=tiled_planes(source,reference,core)
        error=float(np.max(np.abs(whole-tiled)))
        assert np.allclose(whole,tiled,atol=1e-4,rtol=1e-4),(height,width,error)
        output=pixel_shuffle(tiled);assert output.shape==(3*height,3*width)
        plan=list(tiles(height,width,core))
        coverage=np.zeros((height,width),dtype=np.uint8)
        for t in plan:coverage[t.y:t.y+t.height,t.x:t.x+t.width]+=1
        assert np.all(coverage==1)
        result=dict(input=[height,width],output=list(output.shape),tiles=len(plan),max_abs_error=error,
                    seconds=time.monotonic()-started,reference='ONNX Runtime CPU',npu_execution=False)
        results.append(result);print(json.dumps(result),flush=True)
        if min(height,width)>=64:
            fixed=list(fixed_tiles(height,width))
            assert all(t.input_shape==(64,64) for t in fixed)
            actual=tiled_planes(source,reference,plan=fixed)
            error=float(np.max(np.abs(whole-actual)))
            assert np.allclose(whole,actual,atol=1e-4,rtol=1e-4),(height,width,error)
            coverage.fill(0)
            for t in fixed:coverage[t.y:t.y+t.height,t.x:t.x+t.width]+=1
            assert np.all(coverage==1)
            result=dict(input=[height,width],static_tile=[64,64],tiles=len(fixed),max_abs_error=error,
                        reference='ONNX Runtime CPU',npu_execution=False)
            results.append(result);print(json.dumps(result),flush=True)
    # Prove exact channel-to-pixel placement, independently of inference.
    p=np.arange(9*2*4).reshape(1,9,2,4)
    out=pixel_shuffle(p)
    for y in range(2):
        for x in range(4):
            for dy in range(3):
                for dx in range(3):assert out[y*3+dy,x*3+dx]==p[0,dy*3+dx,y,x]
    (ROOT/'results/superres-image-tests.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':main()
