# Setup and Reproduction

## 1. Prerequisites

- **Kernel**: ARM64 Linux kernel (`>= 6.12`) with matching headers and the [`NVDA8600` `_HRV=1` noncoherent DMA quirk](../patches/arm64-acpi-iort-nvda8600-noncoherent-dma.patch) applied.
- **Host Packages (Arch/ARM64)**: `base-devel`, `python`, `uv`, `openssl`, `7zip`, and CUDA (`/opt/cuda`) for the optional cuDLA backend.

## 2. Checkout and Python Environment

```sh
git clone https://github.com/johannhartmann/n1x-npu-linux.git
cd n1x-npu-linux
uv venv --python 3.12 venv
uv pip install --python venv/bin/python --require-hashes --only-binary :all: -r requirements.lock
mkdir -p sources results
make check
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

Extract and verify the SROOT and DLA firmware images:

```sh
venv/bin/python scripts/prepare_inputs.py --extract-firmware
venv/bin/python scripts/prepare_inputs.py
```

## 4. Build Userspace Runtime and Compiler

Build the native `t254` compiler (`runtime/libn1x_compiler.so`, `runtime/n1x-compile`), heterogeneous ONNX runtime (`runtime/libn1x_ort.so`, `runtime/n1x-ort-run`), and cuDLA backend (`runtime/cudla/libnvcudla.so`):

```sh
make runtime cudla
make test-compiler
make test-cudla
```

## 5. Install Firmware and Build Kernel Module

Install the extracted SROOT firmware images and compile `n1x_npu_bootstrap.ko`:

```sh
sudo install -Dm600 sources/sroot-v0.bin /usr/lib/firmware/nvidia/n1x/sroot-v0.bin
sudo install -Dm600 sources/sroot-v1.bin /usr/lib/firmware/nvidia/n1x/sroot-v1.bin
make module
```

`n1x_npu_bootstrap.ko` locates and parses the VBIOS policy directly from the live hardware carveout (`NVDA3000` locator `0x1182bc80` $\rightarrow$ `0xe5b81800`), so no `.rom` file is needed in `/usr/lib/firmware`.

## 6. Hardware Qualification

To run the full startup, inference, shutdown, and reload qualification sequence:

```sh
pkexec "$PWD/venv/bin/python" "$PWD/scripts/qualify_npu_reload.py" \
  --cycles 2 --output-dir "$PWD/results/NEW-reload"
```

## 7. Enable Automatic Startup at Boot

Build and install the `n1x-npu-driver` Arch package (see [updates](updates.md)), then enable the systemd service:

```sh
sudo systemctl enable --now n1x-npu.service
```

[`99-n1x-npu.rules`](../packaging/udev/99-n1x-npu.rules) and [`n1x-npu.service`](../packaging/systemd/n1x-npu.service) switch the `NVDA200A:00` and `NVDA8600:00` IOMMU groups from `identity` to `DMA`, load `n1x_npu_bootstrap`, and expose `/dev/accel/accel0` (`0660 root:render`, `TAG+="uaccess"`).
