# Setup and Reproduction

## 1. Prerequisites

- **Kernel**: `7.2.8-23-rtx-spark-npu` with matching headers (see [P16 Linux setup](https://github.com/johannhartmann/asus-proart-p16-rtx-spark-linux) and [`patches/linux-7.2.8-p16-sroot-noncoherent-dma.patch`](../patches/linux-7.2.8-p16-sroot-noncoherent-dma.patch)).
- **Host Packages (Arch/ARM64)**: `base-devel`, `python`, `uv`, `bubblewrap`, `gdb`, `openssl`, `libdrm`, `acpica`, `7zip`, and CUDA (`/opt/cuda`) for the optional cuDLA backend.

## 2. Checkout and Python Environments

```sh
git clone https://github.com/johannhartmann/n1x-npu-linux.git
cd n1x-npu-linux
uv venv --python 3.12 venv
uv pip install --python venv/bin/python --require-hashes --only-binary :all: \
  -r requirements.lock -r requirements-probe.lock
mkdir -p sources results
make check
```

Optional image environment for `./demo-superres`:

```sh
uv venv --python 3.12 sources/superres-venv
uv pip install --python sources/superres-venv/bin/python --require-hashes \
  --only-binary :all: -r requirements-superres.lock
```

## 3. Acquire Pinned Inputs

Download the public cuDLA archive (`13.4.92`), Linux ARM64 TensorRT compiler package (`11.4`), ONNX Model Zoo models (`super-resolution-10.onnx`, `emotion-ferplus-8.onnx`, `mnist-12.onnx`), Windows ARM64 `nvdla_compiler.dll`, and sample image:

```sh
venv/bin/python scripts/prepare_inputs.py --fetch
```

Unpack the **NVIDIA 617.42 Windows ARM64** driver (`SHA-256 425a75f54655ddcc8ee6859cec49bc431a17ef18094daaf59f205b159fb6e2bd`) into `sources/nvidia/`:

```sh
7z x /absolute/path/to/NVIDIA-617.42-ARM64.exe -osources/nvidia 'Display.Driver/*'
```

| Required File | SHA-256 |
| --- | --- |
| `sources/nvidia/Display.Driver/nvlddmkm.sys` | `73b3187cdf84c0306df78595e49626d096e84dfc1e6356543db57b145007f987` |
| `sources/nvidia/Display.Driver/nvdla_runtime.dll` | `c58e3425eeee93f89b1ae68f814552aa1cacaaf85d913ca12a9b14f83902d49b` |

Extract and verify the SROOT and DLA firmware images:

```sh
venv/bin/python scripts/prepare_inputs.py --extract-firmware
venv/bin/python scripts/prepare_inputs.py
```

## 4. Build Userspace Runtime, Compiler, and Models

Build the native `t254` compiler (`runtime/libn1x_compiler.so`, `runtime/n1x-compile`), heterogeneous ONNX runtime (`runtime/libn1x_ort.so`, `runtime/n1x-ort-run`), and cuDLA backend (`runtime/cudla/libnvcudla.so`):

```sh
make runtime cudla
make test-compiler
make test-cudla
```

To reproduce the four hash-pinned cuDLA fixture loadables (`relu`, `conv1x1`, `conv1x1_relu`, `superres`):

```sh
venv/bin/python scripts/build_models.py --superres
```

## 5. Install Firmware and Build Kernel Module

Install the extracted SROOT firmware images and compile `n1x_npu_bootstrap.ko`:

```sh
sudo install -Dm600 sources/dla-p16-sroot-v0.bin /usr/lib/firmware/nvidia/n1x/p16-sroot-v0.bin
sudo install -Dm600 sources/dla-p16-sroot-v1.bin /usr/lib/firmware/nvidia/n1x/p16-sroot-v1.bin
make module
```

By default (`strict_platform=0`), `n1x_npu_bootstrap.ko` reads the VBIOS policy directly from the live hardware carveout (`0x1182bc80`), so no `.rom` file is needed in `/usr/lib/firmware`.

To install the optional privileged cuDLA broker for `./demo-superres`, `./demo-denoise`, and `./demo-classify`:

```sh
pkexec /usr/bin/python3 "$PWD/scripts/install_cudla_broker.py" --models superres
```

## 6. Hardware Qualification

To run the full startup, inference, shutdown, and reload qualification sequence on the P16:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_p16_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

## 7. Enable Automatic Startup at Boot

Build and install the `n1x-npu-bootstrap-rtx-spark` Arch package (see [updates](updates.md)), then enable the systemd service:

```sh
sudo systemctl enable --now n1x-npu.service
```

[`99-n1x-npu.rules`](../packaging/udev/99-n1x-npu.rules) and [`n1x-npu.service`](../packaging/systemd/n1x-npu.service) switch the `NVDA200A:00` and `NVDA8600:00` IOMMU groups from `identity` to `DMA`, load `n1x_npu_bootstrap` (`autostart=1`), and expose `/dev/accel/accel0` (`0660 root:render`, `TAG+="uaccess"`).
