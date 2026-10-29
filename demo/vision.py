#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Physical N1X DnCNN denoising and ResNet-18 classification with CPU validation.

Several images share loaded NPU models and bounded retained backing. There is
no learned CPU fallback, automatic retry, reset, unload or power-profile change.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import sys
import time
import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from cudla_broker import Device, FRONTEND_SHA256, BROKER
from loadable import Model
from vision_image import classify_input, denoise, psnr, reference, resnet18, ssim


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def predictions(logits):
    labels = (ROOT/'sources/vision/imagenet_classes.txt').read_text().splitlines()
    if len(labels)!=1000 or logits.shape!=(1,1000): raise ValueError('invalid classifier output/labels')
    scores = np.exp(logits[0]-np.max(logits[0])); scores /= scores.sum()
    return [dict(index=int(i),label=labels[i],probability=float(scores[i])) for i in np.argsort(-scores)[:5]]


def prepare_image(example,path,add_noise):
    if example=='classify':
        image,data = classify_input(path)
        return image,data,None
    with Image.open(path) as source: image = ImageOps.exif_transpose(source).convert('L')
    clean = np.asarray(image,dtype=np.float32)/255
    if min(clean.shape)<96: raise ValueError('denoising requires at least 96 pixels on each axis')
    data = clean.copy()
    if add_noise: data += np.random.default_rng(0).normal(0,25/255,data.shape).astype(np.float32)
    return image,data.astype(np.float16).astype(np.float32),clean


def run_image(args,path,prepared,device,networks,destination,metadata,growth):
    image,data,clean = prepared
    destination.mkdir(parents=True,exist_ok=False)
    def record(name,value):
        with (destination/name).open('x') as output:
            json.dump(value,output,indent=2,allow_nan=False); output.write('\n'); output.flush(); os.fsync(output.fileno())
    def save(name,value): value.save(destination/name)
    before = device.info()
    metadata = dict(metadata,input=str(path.resolve()),input_sha256=sha(path),input_shape=list(data.shape),
                    transport_before=before,planned_retained_growth_bytes=growth)
    record('attempt.json',metadata)
    completed = 0; attempted = 0
    def infer(name,values):
        nonlocal completed,attempted
        attempted += 1
        output = networks[name](values); completed += 1
        if not np.isfinite(output).all(): raise ValueError('nonfinite NPU output')
        return output
    print(f'{args.example}: {path}, physical N1X DLA',flush=True)
    started = time.monotonic()
    try:
        if args.example=='denoise':
            def progress(index,total):
                if index%10==0 or index==total: print(f'DLA windows: {index}/{total}',flush=True)
            result,noise,windows = denoise(data,infer,progress)
        else:
            with np.load(ROOT/'sources/vision/weights.npz',allow_pickle=False) as weights:
                result = resnet18(data,infer,weights)
    except Exception as exc:
        record('error.json',dict(error=str(exc),attempted_submissions=attempted,completed_submissions=completed,
                                 submission_state_uncertain=attempted!=completed,automatic_retry=False))
        raise
    elapsed = time.monotonic()-started
    after = device.info()
    jobs = len(networks) if growth else 0
    if after['retained_jobs']!=before['retained_jobs']+jobs or after['retained_bytes']!=before['retained_bytes']+growth:
        raise RuntimeError('unexpected retained allocation growth; stopped')
    metadata.update(dla_pipeline_seconds=elapsed,submissions=completed,transport_after=after)
    np.save(destination/'dla-output.npy',result)
    save('input.png',image)
    reference_started = time.monotonic()
    if args.example=='denoise':
        np.save(destination/'noisy-input.npy',data)
        def gray(a): return Image.fromarray(np.rint(np.clip(a,0,1)*255).astype(np.uint8))
        save('noisy.png',gray(data)); save('denoised.png',gray(result))
        expected_noise = reference('dncnn-noise.onnx',data[None,None])[0,0]
        difference = noise.astype(np.float64)-expected_noise
        metadata.update(windows=windows,noise_model_sigma=25,synthetic_noise=args.add_noise,
                        synthetic_seed=0 if args.add_noise else None,
                        host_operations=['grayscale conversion','tiling/stitching','noise subtraction'],
                        acceptance=dict(max_abs=.01,rmse=.001))
        if args.add_noise:
            metadata['quality'] = dict(noisy_psnr_db=psnr(np.clip(data,0,1),clean),
                denoised_psnr_db=psnr(np.clip(result,0,1),clean),
                noisy_ssim=ssim(np.clip(data,0,1),clean),denoised_ssim=ssim(np.clip(result,0,1),clean))
        pictures = [('Original / input','input.png'),('Noisy input','noisy.png'),('NPU denoised','denoised.png')]
    else:
        expected = reference('resnet18.onnx',data)
        difference = result.astype(np.float64)-expected
        top5 = predictions(result); record('predictions.json',top5)
        metadata.update(top5=top5,reference_top1=int(expected.argmax()),
            top1_agrees=bool(int(result.argmax())==int(expected.argmax())),
            host_operations=['resize/center crop/normalization','im2col gather','max/average pooling','residual add/ReLU','fully connected head','softmax'],
            acceptance=dict(max_abs=.25,rmse=.05,top1_agrees=True))
        pictures = [('Classifier input (224x224 center crop)','input.png')]
    maximum = float(np.max(np.abs(difference))); rmse = float(np.sqrt(np.mean(difference*difference)))
    passed = maximum<=metadata['acceptance']['max_abs'] and rmse<=metadata['acceptance']['rmse']
    if args.example=='classify': passed = passed and metadata['top1_agrees']
    if args.add_noise: passed = passed and metadata['quality']['denoised_psnr_db']>metadata['quality']['noisy_psnr_db']
    metadata.update(reference_seconds=time.monotonic()-reference_started,maximum_absolute_error=maximum,
                    rmse=rmse,reference_passed=passed)
    record('result.json',metadata)
    panels = ''.join(f'<section><h2>{html.escape(label)}</h2><a href="{name}"><img src="{name}"></a></section>' for label,name in pictures)
    title = 'N1X NPU '+args.example
    (destination/'report.html').write_text(f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
        '<style>body{background:#111;color:#eee;font:16px system-ui;margin:32px}main{display:flex;gap:20px}section{flex:1}img{max-width:100%}pre{white-space:pre-wrap}</style>'
        f'<h1>{title}</h1><p>Physical NPU convolutions. CPU reference: {"PASS" if passed else "FAIL"}.</p>'
        f'<main>{panels}</main><pre>{html.escape(json.dumps(metadata,indent=2))}</pre>')
    if not passed: raise RuntimeError('independent CPU reference/quality check failed; evidence retained')
    print(f'PASS: {destination/"report.html"}; {completed} NPU submissions, max error {maximum:.6g}, RMSE {rmse:.6g}',flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('example',choices=('denoise','classify'))
    parser.add_argument('images',type=Path,nargs='+')
    parser.add_argument('--output-dir',required=True,type=Path,help='new directory; batches have numbered subdirectories')
    parser.add_argument('--add-noise',action='store_true',help='denoise only: Gaussian noise, sigma 25/255, seed 0')
    args = parser.parse_args()
    if args.add_noise and args.example!='denoise': parser.error('--add-noise is for denoising only')
    if os.geteuid()==0: parser.error('run as an unprivileged desktop user; the installed broker isolates RAWIO')
    if platform.release()!='7.2.8-23-omarchy-p16-asus-npu2': parser.error('requires the audited npu2 kernel')
    manifest = json.loads((ROOT/'config/vision-models.json').read_text())
    for name,expected in manifest['prepared'].items():
        if sha(ROOT/'sources/vision'/name)!=expected: parser.error('prepared source mismatch: '+name)
    names = [name for name in manifest['models'] if name.startswith('denoise-')==(args.example=='denoise')]
    expected_names = ({f'denoise-{i}' for i in range(3)} if args.example=='denoise' else
                      {'resnet18-stem',*(f'resnet18-b{i}' for i in range(8)),*(f'resnet18-down{i}' for i in (2,4,6))})
    if set(names)!=expected_names: parser.error('incomplete or unexpected model manifest; no inference attempted')
    models = {}; required = 0
    for name in names:
        path = ROOT/'results'/('native-compile-t254-'+name)/(name+'.nvda')
        if sha(path)!=manifest['models'][name]['sha256']: parser.error('loadable mismatch: '+name)
        model = Model.from_file(path); models[name] = path
        required += (len(model.image)+8192+4095)&~4095
    # Validate every input before any model can submit; decode one image at a
    # time below to keep host memory bounded for long batches.
    for path in args.images:
        with Image.open(path) as image:
            image.verify()
            if args.example=='denoise' and min(image.size)<96: parser.error('denoising requires at least 96 pixels on each axis')
    destination = args.output_dir.resolve()
    if destination.exists(): parser.error('output directory must be new')
    batch = len(args.images)>1
    if batch:
        destination.mkdir(parents=True,exist_ok=False)
    metadata = dict(example=args.example,kernel=platform.release(),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        module_srcversion=Path('/sys/module/n1x_npu_bootstrap/srcversion').read_text().strip(),
        platform_profile=Path('/sys/firmware/acpi/platform_profile').read_text().strip(),
        frontend_sha256=FRONTEND_SHA256,
        models={name:manifest['models'][name] for name in names},prepared=manifest['prepared'],
        total_required_retained_bytes=required,automatic_retry=False,
        learned_convolutions='physical N1X DLA via original NVIDIA cuDLA')
    with Device(models) as device:
        metadata['broker_sha256']=sha(BROKER)
        before=device.info()
        if not before['capabilities']&4: parser.error('driver lacks completed-task I/O reuse')
        if required>before['retain_limit']-before['retained_bytes'] or len(models)>before['max_jobs']-before['retained_jobs']:
            parser.error('insufficient retained allocation budget; no submission. See docs/shutdown-reload.md.')
        networks = {name:device.load(name) for name in models}
        for index,path in enumerate(args.images):
            target = destination/f'{index+1:04d}' if batch else destination
            run_image(args,path,prepare_image(args.example,path,args.add_noise),device,networks,target,
                      metadata,required if index==0 else 0)


if __name__=='__main__': main()
